"""
core/weight_calibration.py

秤重校正的計算（tools/calibrate_weight.py 負責錄音與互動，這裡只做純計算，可以離線
用 --from-data 重算、也可以用合成資料測試）。

跟門口校正同一個原則：參數不是用公式推「應該夠」，而是把錄到的原始讀數**丟進執行期
同一份程式（core/cart_state_machine.CartStateMachine）重播**，看實際行為對不對。

錄音的段落（label）：
    zero_start        空秤靜止
    load_1, load_2…   放上已知重量的砝碼靜止（meta 裡有 mass_g）
    zero_end          全部拿掉、空秤靜止（看零點有沒有回來）
    push_empty        籃子空的，推車照平常的方式在店裡推（看推車震動有多大）
    drop_1, drop_2…   掃完碼後把砝碼像平常放商品一樣放進籃子（meta 有 mass_g）

符號（下面的說明與輸出都用這幾個）：
    r          HX711 原始讀數（count）
    m          砝碼質量（g）
    a, b       換算參數：r = a + b·m；a = hx711_offset、b = hx711_scale（count/g）
    w          換算後的重量 (r − a) / b（g）
    σ_s        靜止時單筆讀數的標準差（g），各靜止段合併
    T          未掃碼門檻 weight.unscanned_change_threshold_g（g）

計算：
    1. 每個靜止段丟掉前 SETTLE_SEC 秒（剛放上去還在晃），取中位數 r̃ 與穩健標準差
       σ = 1.4826·MAD（MAD = 與中位數差的絕對值的中位數）。用中位數而不是平均：
       2026-11-16 實測，按 Enter 時手還壓在砝碼上，前 1.5 秒讀數多了 75 g，平均值被
       拉偏 8.7 g、標準差變成 29 g，門檻因此被算成 87 g；中位數與 MAD 不受這種少數
       偏離影響。段落前半與後半的中位數差太多（還沒放穩）會另外提示
    2. a、b：所有靜止段 (m, r̃) 的最小平方直線；殘差換算成 g 看線性
       （只有一個砝碼時兩點決定一條線，看不出線性，判讀最多「勉強」）
    3. 零點漂移：zero_end 與 zero_start 的 r̄ 差，換成 g
    4. T：把 push_empty 的讀數換成 g，從小到大試 T，丟進 CartStateMachine（SHOPPING 狀態）
       重播，找出「推車震動不會誤觸發未掃碼警告」的最小 T_min，建議
           T = max(⌈1.5·T_min⌉, ⌈6σ_s⌉, T_floor)
       T_floor（預設 10 g）是人為下限：雜訊很小時前兩項可能只有 2 g，但手扶籃子、
       掛個購物袋這種不是商品的小變化不該觸發警告，這一項不是量出來的，是取捨
    5. 等穩定的參數（狀態機的 settle_window_sec = W_s、settle_band_g = B_s，定義見
       core/cart_state_machine.py 開頭）：B_s = max(2, ⌈6σ_s⌉)；W_s 從 0（不等，舊版行為）、
       0.2、0.3 … 1.0 秒依序試，每一個 W_s 都重新做第 4 步定 T、再重播全部放入，
       找「每一趟都正常加入、加入後也沒有誤報」的最小 W_s，再往上多取一格當餘裕
       （下一格也全部通過才取；放入測試只有幾趟，剛好過的那一格不保險）
    6. 放入測試：每一趟 drop 都用 CartStateMachine 重播「掃碼 → 放入」：
       商品重量 = 砝碼重量、容差 = 10%（跟 tools/product_admin.py 建檔時的自動容差同一個比例），
       看是不是正常加入、有沒有判成重量不符或逾時、加入後有沒有因為晃動又誤報未掃碼

注意：秤重比對只看「重量變化」（放入前後的差），所以真正決定比對準不準的是 b；
a 只影響畫面上顯示的絕對重量。
"""

from __future__ import annotations

import math
import statistics
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from core.cart_manager import CartManager
from core.cart_state_machine import (
    ALERT_UNSCANNED_WEIGHT_CHANGE,
    ALERT_WEIGHT_DIRECTION_MISMATCH,
    ALERT_WEIGHT_MATCH_TIMEOUT,
    ALERT_WEIGHT_MISMATCH,
    CartStateMachine,
    GateEntryDetected,
    ItemScanned,
    LoginScanned,
    TimeoutTick,
    WeightSampleReceived,
)
from core.uart_recording import Recording, Sample
from database.db_manager import DBManager, Member, Product

SETTLE_SEC = 1.0
SAFETY = 1.5
SIGMA_K = 6.0
T_FLOOR = 10
MAD_TO_SIGMA = 1.4826
SETTLE_WINDOWS = (0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0)
SETTLE_BAND_MIN = 2.0
T_MAX = 300
PRODUCT_TOLERANCE_RATIO = 0.10    # 跟 tools/product_admin.py 的 _AUTO_TOLERANCE_RATIO 一致
DROP_SCAN_AT = 0.5                # drop 段開始後幾秒視為「掃碼」（工具提示 1 秒後才放）
GOOD_RESIDUAL_G = 1.0
GOOD_RESIDUAL_RATIO = 0.005

_MEMBER = "MEMBER-CAL"
_ITEM = "CAL-ITEM"


# ----------------------------------------------------------------------
@dataclass
class StaticPoint:
    label: str
    mass_g: float
    level_raw: float      # 中位數
    sigma_raw: float      # 1.4826·MAD
    n: int
    residual_g: float = 0.0
    drift_raw: float = 0.0    # 後半中位數 − 前半中位數


@dataclass
class PushStats:
    n: int
    max_abs_g: float
    p99_abs_g: float
    std_g: float


@dataclass
class DropResult:
    label: str
    mass_g: float
    outcome: str                 # 正常加入 / 判成重量不符 / 逾時 / 錄音結束前沒判出
    commit_after_sec: Optional[float]
    overshoot_g: float           # 放入過程中最大值 − 最後穩定值
    extra_alerts: int            # 加入之後又誤報「未掃碼的重量變化」幾次
    detail: str = ""


@dataclass
class WeightCalibration:
    error: Optional[str]
    verdict: str                 # 可以 / 勉強 / 不夠 / 錯誤
    offset: Optional[float] = None
    scale: Optional[float] = None
    points: List[StaticPoint] = field(default_factory=list)
    sigma_static_g: Optional[float] = None
    zero_drift_g: Optional[float] = None
    push: Optional[PushStats] = None
    t_min: Optional[int] = None
    t_rec: Optional[int] = None
    t_reason: str = ""
    drops: List[DropResult] = field(default_factory=list)
    light_products: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    settle_window_sec: Optional[float] = None
    settle_band_g: Optional[float] = None
    settle_table: List[dict] = field(default_factory=list)   # 每個 W_s 的結果


# ----------------------------------------------------------------------
def steady_raw(samples: Sequence[Sample], settle: float = SETTLE_SEC) -> List[int]:
    if not samples:
        return []
    t0 = samples[0].t
    return [s.hx711_raw for s in samples if s.t >= t0 + settle]


def static_points(rec: Recording) -> List[StaticPoint]:
    pts = []
    for seg in rec.segments:
        if seg.label.startswith("zero"):
            mass = 0.0
        elif seg.label.startswith("load"):
            mass = float(seg.info.get("mass_g", 0.0))
        else:
            continue
        raw = steady_raw(rec.get(seg.label))
        if len(raw) < 5:
            continue
        med = statistics.median(raw)
        mad = statistics.median(abs(v - med) for v in raw)
        half = len(raw) // 2
        p = StaticPoint(seg.label, mass, med, MAD_TO_SIGMA * mad, len(raw))
        p.drift_raw = statistics.median(raw[half:]) - statistics.median(raw[:half])
        pts.append(p)
    return pts


def fit_line(points: Sequence[StaticPoint]):
    """最小平方 r = a + b·m。回傳 (a, b)；質量少於兩種時回傳 None。"""
    ms = [p.mass_g for p in points]
    if len(set(ms)) < 2:
        return None
    m_bar = statistics.fmean(ms)
    r_bar = statistics.fmean(p.level_raw for p in points)
    sxx = sum((m - m_bar) ** 2 for m in ms)
    sxy = sum((p.mass_g - m_bar) * (p.level_raw - r_bar) for p in points)
    b = sxy / sxx
    return r_bar - b * m_bar, b


def to_grams(samples: Sequence[Sample], a: float, b: float) -> List[float]:
    return [(s.hx711_raw - a) / b for s in samples]


# ----------------------------------------------------------------------
# 用執行期的狀態機重播
# ----------------------------------------------------------------------
def _machine(db_path: str, threshold: float, sm_cfg: dict, item_g: float = 100.0) -> CartStateMachine:
    db = DBManager(db_path)
    db.init_db(seed=False)
    db.upsert_member(Member(_MEMBER, "校正用"))
    db.upsert_product(Product(_ITEM, "校正砝碼", 0.0, item_g, item_g * PRODUCT_TOLERANCE_RATIO))
    cfg = dict(sm_cfg)
    cfg["unscanned_change_threshold_g"] = threshold
    cfg["login_barcode_prefix"] = "MEMBER-"
    return CartStateMachine(db=db, cart=CartManager(), config=cfg)


def _shopping(sm: CartStateMachine, t: float) -> None:
    sm.process_event(LoginScanned(member_id=_MEMBER, timestamp=t))
    sm.process_event(GateEntryDetected(timestamp=t))


def replay_push(times: Sequence[float], grams: Sequence[float], threshold: float, sm_cfg: dict) -> int:
    """推車震動重播：回傳誤觸發「未掃碼的重量變化」幾次。"""
    with tempfile.TemporaryDirectory() as d:
        sm = _machine(str(Path(d) / "cal.db"), threshold, sm_cfg)
        _shopping(sm, times[0])
        for t, g in zip(times, grams):
            sm.process_event(WeightSampleReceived(grams=g, timestamp=t))
            sm.process_event(TimeoutTick(now=t))
        return sum(1 for a in sm.get_alerts() if a.code == ALERT_UNSCANNED_WEIGHT_CHANGE)


def min_threshold(times, grams, sm_cfg: dict, start: int) -> Optional[int]:
    for t_g in range(max(1, start), T_MAX + 1):
        if replay_push(times, grams, float(t_g), sm_cfg) == 0:
            return t_g
    return None


def replay_drop(label: str, samples: Sequence[Sample], mass_g: float, a: float, b: float,
                threshold: float, sm_cfg: dict) -> DropResult:
    grams = to_grams(samples, a, b)
    times = [s.t for s in samples]
    t_scan = times[0] + DROP_SCAN_AT
    tail = [g for t, g in zip(times, grams) if t >= times[-1] - 1.0] or grams[-1:]
    final = statistics.median(tail)
    base = statistics.median([g for t, g in zip(times, grams) if t < t_scan] or grams[:1])
    overshoot = max(grams) - final
    with tempfile.TemporaryDirectory() as d:
        sm = _machine(str(Path(d) / "cal.db"), threshold, sm_cfg, item_g=mass_g)
        _shopping(sm, times[0])
        scanned = False
        commit_t = None
        bad = None
        n_before = 0
        for t, g in zip(times, grams):
            if not scanned and t >= t_scan:
                sm.process_event(ItemScanned(barcode=_ITEM, timestamp=t))
                scanned = True
            sm.process_event(WeightSampleReceived(grams=g, timestamp=t))
            sm.process_event(TimeoutTick(now=t))
            if commit_t is None and sm.cart.has_item(_ITEM):
                commit_t = t
                n_before = sum(1 for x in sm.get_alerts() if x.code == ALERT_UNSCANNED_WEIGHT_CHANGE)
            if bad is None:
                for x in sm.get_alerts():
                    if x.code in (ALERT_WEIGHT_MISMATCH, ALERT_WEIGHT_DIRECTION_MISMATCH, ALERT_WEIGHT_MATCH_TIMEOUT):
                        bad = x
        extra = 0
        if commit_t is not None:
            extra = sum(1 for x in sm.get_alerts() if x.code == ALERT_UNSCANNED_WEIGHT_CHANGE) - n_before
    if bad is not None and bad.code == ALERT_WEIGHT_MATCH_TIMEOUT:
        outcome = "逾時"
    elif bad is not None:
        outcome = "判成重量不符"
    elif commit_t is not None:
        outcome = "正常加入"
    else:
        outcome = "錄音結束前沒判出"
    detail = f"放入前 {base:.1f} g → 穩定後 {final:.1f} g（變化 {final - base:+.1f} g，砝碼 {mass_g:g} g）"
    if bad is not None:
        detail += f"；{bad.message}"
    return DropResult(label, mass_g, outcome, None if commit_t is None else commit_t - t_scan,
                      overshoot, extra, detail)


# ----------------------------------------------------------------------
def _pick_t(t_min: Optional[int], sigma_g: float, t_floor: int):
    terms = {"推車震動 1.5·T_min": int(math.ceil(SAFETY * t_min)) if t_min is not None else 0,
             "靜止雜訊 6σ_s": int(math.ceil(SIGMA_K * sigma_g)),
             "人為下限 T_floor": int(t_floor)}
    name = max(terms, key=terms.get)
    return terms[name], "、".join(f"{k} = {v}" for k, v in terms.items()) + f"，取最大的（{name}）"


def calibrate(rec: Recording, sm_cfg: dict, product_weights: Optional[Dict[str, float]] = None,
              t_floor: int = T_FLOOR) -> WeightCalibration:
    pts = static_points(rec)
    if not any(p.label.startswith("zero") for p in pts):
        return WeightCalibration("沒有空秤的資料（zero_start）。", "錯誤", points=pts)
    if not any(p.mass_g > 0 for p in pts):
        return WeightCalibration("至少要放一個已知重量的砝碼。", "錯誤", points=pts)
    line = fit_line(pts)
    if line is None:
        return WeightCalibration("至少要有空秤與一個砝碼兩種重量。", "錯誤", points=pts)
    a, b = line
    if abs(b) < 1e-9:
        return WeightCalibration("放上砝碼前後讀數沒有變化——檢查砝碼有沒有放上去、HX711 接線。", "錯誤", points=pts)
    for p in pts:
        p.residual_g = (p.level_raw - (a + b * p.mass_g)) / b
    cal = WeightCalibration(None, "", offset=a, scale=b, points=pts)

    # 靜止雜訊（合併各段的變異數）
    num = sum(p.sigma_raw ** 2 * p.n for p in pts)
    den = sum(p.n for p in pts)
    cal.sigma_static_g = math.sqrt(num / den) / abs(b)

    zs = {p.label: p for p in pts if p.label.startswith("zero")}
    if "zero_start" in zs and "zero_end" in zs:
        cal.zero_drift_g = (zs["zero_end"].level_raw - zs["zero_start"].level_raw) / b
    for p in pts:
        moved = p.drift_raw / b
        if abs(moved) > max(1.0, 3 * cal.sigma_static_g):
            cal.issues.append(f"{p.label} 錄的過程中讀數還在變（後半比前半 {moved:+.1f} g）——還沒放穩、手還碰著，"
                              "或秤台在潛變。已經用中位數，影響不大，但這一點的可信度比較低。")

    # 推車震動、等穩定、放入測試
    push = rec.get("push_empty")
    drop_segs = [seg for seg in rec.segs("drop") if len(rec.get(seg.label)) >= 10]
    if len(push) >= 10:
        g = to_grams(push, a, b)
        med = statistics.median(g)
        dev = sorted(abs(x - med) for x in g)
        cal.push = PushStats(len(g), dev[-1], dev[min(len(dev) - 1, int(0.99 * len(dev)))], statistics.pstdev(g))
    else:
        cal.issues.append("沒有推車震動的資料（push_empty），不重新定未掃碼門檻，沿用原本的。")
    band = float(max(SETTLE_BAND_MIN, math.ceil(SIGMA_K * cal.sigma_static_g)))
    windows = SETTLE_WINDOWS if drop_segs else (float(sm_cfg.get("settle_window_sec", 0.0) or 0.0),)
    chosen = None
    for w in windows:
        cfg_w = {**sm_cfg, "settle_window_sec": w, "settle_band_g": band}
        t_min = t_rec = None
        reason = ""
        if cal.push is not None:
            t_min = min_threshold([s.t for s in push], g, cfg_w, 1)
            if t_min is not None:
                t_rec, reason = _pick_t(t_min, cal.sigma_static_g, t_floor)
        thr = float(t_rec or sm_cfg.get("unscanned_change_threshold_g", T_FLOOR))
        drops = [replay_drop(seg.label, rec.get(seg.label), float(seg.info.get("mass_g", 0.0)), a, b, thr, cfg_w)
                 for seg in drop_segs]
        ok = [d for d in drops if d.outcome == "正常加入" and not d.extra_alerts]
        lat = [d.commit_after_sec for d in drops if d.commit_after_sec is not None]
        row = {"window": w, "t_min": t_min, "t_rec": t_rec, "t_reason": reason, "ok": len(ok), "n": len(drops),
               "latency": statistics.fmean(lat) if lat else None, "drops": drops}
        cal.settle_table.append(row)
    passing = [r for r in cal.settle_table
               if r["n"] and r["ok"] == r["n"] and (cal.push is None or r["t_min"] is not None)]
    if passing:
        i = cal.settle_table.index(passing[0])
        nxt = cal.settle_table[i + 1] if i + 1 < len(cal.settle_table) else None
        chosen = nxt if nxt in passing else passing[0]
    if chosen is None:
        # 沒有一組全過：用過最多趟的那組（同分取小的 W_s），判讀會是不夠
        chosen = max(cal.settle_table, key=lambda r: (r["ok"], -r["window"]))
    cal.t_min, cal.t_rec, cal.t_reason, cal.drops = chosen["t_min"], chosen["t_rec"], chosen["t_reason"], chosen["drops"]
    if drop_segs:
        cal.settle_window_sec, cal.settle_band_g = chosen["window"], band
    if cal.push is not None and cal.t_min is None:
        cal.issues.append(f"推車時的晃動超過 {T_MAX} g，任何門檻都會誤報——檢查秤台是不是有東西碰到車架。")

    # 太輕、偷放偵測不到的商品
    if product_weights and cal.t_rec:
        cal.light_products = sorted(f"{n}（{w:g} g）" for n, w in product_weights.items() if w <= cal.t_rec)

    # 判讀
    masses = {p.mass_g for p in pts if p.mass_g > 0}
    max_mass = max(masses)
    worst = max(abs(p.residual_g) for p in pts)
    lin_ok = worst <= max(GOOD_RESIDUAL_G, GOOD_RESIDUAL_RATIO * max_mass)
    if len(masses) < 2:
        cal.issues.append("只有一種砝碼重量，量不出線性（兩點必定共線）。再加一個不同重量的砝碼會比較可靠。")
    elif not lin_ok:
        loads = sorted(p.mass_g for p in pts if p.mass_g > 0)
        cal.issues.append(
            f"線性殘差最大 {worst:.1f} g，超過 {max(GOOD_RESIDUAL_G, GOOD_RESIDUAL_RATIO * max_mass):.1f} g。"
            "可能是秤真的不線性、砝碼的標示重量不準，或放的位置不同讀數不同。"
            f"分辨方法：把 {loads[0]:g} g 和 {loads[-1]:g} g 一起放上去錄一點（{loads[0] + loads[-1]:g} g）——"
            "如果讀數的增加量剛好等於兩個各自增加量的和，秤是線性的，問題在砝碼標示。")
    if cal.zero_drift_g is not None and abs(cal.zero_drift_g) > max(2.0, 3 * cal.sigma_static_g):
        cal.issues.append(f"拿掉砝碼後零點差了 {cal.zero_drift_g:+.1f} g（潛變或機構沒回位）。")
    bad_drops = [d for d in cal.drops if d.outcome != "正常加入" or d.extra_alerts]
    if bad_drops:
        cal.issues.append(f"放入測試 {len(bad_drops)}/{len(cal.drops)} 趟不正常（見逐趟結果）。")
        cal.issues.append(f"等穩定的時間試到 {SETTLE_WINDOWS[-1]:g} 秒都還有不正常的放入——看逐趟結果："
                          "是不是放的時候手一直碰著、放完又去動它，或錄音在讀數穩定前就結束了。"
                          "offset、scale 本身不受影響，仍可寫回。")
    if not cal.drops and cal.push is not None:
        cal.issues.append("沒有做放入測試，不知道放商品時的晃動會不會被判成重量不符。")
    if cal.t_min is None and push:
        cal.verdict = "不夠"
    elif len(masses) >= 2 and lin_ok and not bad_drops and (cal.drops or cal.push is None):
        quick_note = cal.push is None and not cal.drops
        others = [s for s in cal.issues if not s.startswith("沒有推車震動")] if quick_note else cal.issues
        cal.verdict = "可以" if not others else "勉強"
    else:
        cal.verdict = "勉強"
    if bad_drops and any(d.outcome != "正常加入" for d in bad_drops):
        cal.verdict = "不夠"
    return cal
