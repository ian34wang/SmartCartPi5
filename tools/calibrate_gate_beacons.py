"""
tools/calibrate_gate_beacons.py

管制區門口 Beacon 的校正工具。判定規則在 core/gate/model.py（跟執行期同一套），
挑參數的計算在 core/gate/calibration.py，這支只負責錄音、互動、寫回。

    校正（預設）：推車實際來回走過門口幾趟，整段連續錄下來，再把每一組候選參數
                  拿去重播判定規則，挑出「每一趟都判對、停在門口不亂跳、偏移量
                  ±2 dB 也成立」而且餘裕最大的那組。詳見 core/gate/calibration.py。
    --survey     ：5 個位置的靜止剖面，不挑參數。用來看佈署形狀（門外、門內的平台
                  夠不夠寬、門檻線附近雜訊多大），調擺法的時候前後各跑一次比較。
    --monitor    ：即時看原始值、濾波值與判定（同時記錄到 data/gate_logs/）。
    --from-data  ：用存下來的錄音重算，不用硬體。

會調的參數：landmarks 區塊的 rssi_estimator、rssi_smoothing_sec、gate_min_confirm_sec、
gate_hysteresis_dbm、gate_min_crossing_rssi_dbm，以及門內那顆的 rssi_offset_db
（gate_min_confirm_samples 不調，沿用現在的值）。

執行期記錄：--from-data 重算時，會自動把「校正錄音之後」錄的執行期記錄
（data/gate_logs/，app 與 --monitor 每次啟動都會產生）一起重播，
排除會「來回跳」（相反的事件間隔不到 3 秒）的參數。所以最好的流程是：先校正寫回、
實際推車用一陣子（特別是在門口附近晃、停、轉彎），再用同一份校正錄音 --from-data
重算一次。

錄的時候：
    - Pi 要跟實際使用時一樣（最好就裝在推車上），推車用平常的速度推。
    - 起點、終點大約在門外、門內 2 m，沿平常進出的路線走。
    - 每一趟停好再按 Enter；按完 Enter 就可以開始推下一趟。

錄音存在 data/gate_calibration/<時間>/（格式跟執行期記錄檔一樣，見 core/gate/recorder.py）。

用法：
    python3 -m tools.calibrate_gate_beacons --dry-run           # 錄音、挑參數，不寫回
    python3 -m tools.calibrate_gate_beacons                     # 同上，最後問要不要寫回
    python3 -m tools.calibrate_gate_beacons --trips 6           # 來回幾次（預設 4，也就是 8 趟）
    python3 -m tools.calibrate_gate_beacons --from-data data/gate_calibration/20261029-150000
    python3 -m tools.calibrate_gate_beacons --from-data <目錄> --no-logs        # 不用執行期記錄
    python3 -m tools.calibrate_gate_beacons --from-data <目錄> --logs-since 20261116-000000
    python3 -m tools.calibrate_gate_beacons --survey            # 5 個位置的靜止剖面
    python3 -m tools.calibrate_gate_beacons --monitor
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from core.gate.calibration import (  # noqa: E402
    DRIFT_DB,
    HOLD_SEC,
    LABELS,
    POSITIONS,
    Calibration,
    Mark,
    Session,
    calibrate_session,
    segment_stats,
    survey_notes,
)
from core.gate.model import FLAP_SEC, GateParams, Observation, flaps  # noqa: E402
from core.gate.recorder import RUNTIME_LOG_ROOT, GateRecorder, load  # noqa: E402
from core.gate.detector import load_landmark_config  # noqa: E402
from drivers.ble_beacon_scanner import (  # noqa: E402
    BleBeaconScanner,
    load_beacon_identity_map,
    load_beacon_ids,
)
from tools._common import drain as _drain, pad as _pad  # noqa: E402

_CAL_ROOT = config.DATA_DIR / "gate_calibration"
_SURVEY_ROOT = config.DATA_DIR / "gate_survey"
_SIDE_NAME = {"a": "門內", "b": "門外", None: "未確認"}
_KIND_NAME = {"entering": "進場", "exiting": "出場"}



# ======================================================================
# 錄音：走過門口
# ======================================================================
class _Tap(threading.Thread):
    """背景把掃描器的封包收進來，標上目前這一段的名稱，同時寫進記錄檔。"""

    def __init__(self, scanner, ids: List[str], recorder: GateRecorder):
        super().__init__(daemon=True)
        self.scanner = scanner
        self.ids = set(ids)
        self.recorder = recorder
        self.label: Optional[str] = None
        self.obs: List[Observation] = []
        self.counts: Dict[str, int] = {}
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                o = self.scanner.out_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            label = self.label
            if label is None or o.beacon_id not in self.ids:
                continue
            self.obs.append((o.timestamp, o.beacon_id, float(o.rssi), None))
            self.counts[o.beacon_id] = self.counts.get(o.beacon_id, 0) + 1
            self.recorder.record_observation(o.timestamp, o.beacon_id, o.rssi, None, None, segment=label)

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=2.0)


def record_walkthrough(scanner, base: GateParams, trips: int, recorder: GateRecorder) -> Optional[Session]:
    ids = [base.inside_id, base.outside_id]
    print("\n" + "=" * 84)
    print(f"走過門口校正：來回 {trips} 次（{trips * 2} 趟），再停門檻線 {HOLD_SEC:.0f} 秒，選做店內深處 {HOLD_SEC:.0f} 秒。")
    print("Pi 要跟實際使用時一樣（最好就裝在推車上），推車用平常的速度推。")
    print("每一趟停好再按 Enter；按完就可以開始推下一趟。")
    print("=" * 84)
    input("  推車停在門外約 2 m（平常進出的路線上），按 Enter 開始錄：")
    _drain(scanner.out_queue)
    tap = _Tap(scanner, ids, recorder)
    tap.start()
    marks: List[Mark] = []

    def segment(label: str, prompt: str) -> str:
        tap.label = label
        t0 = time.time()
        ans = input(prompt).strip().lower()
        marks.append(Mark(label, t0, time.time()))
        return ans

    try:
        for i in range(1, trips + 1):
            segment(f"enter{i}", f"  第 {2 * i - 1}/{trips * 2} 趟（進場）：推到門內約 2 m 停好，按 Enter：")
            segment(f"exit{i}", f"  第 {2 * i}/{trips * 2} 趟（出場）：推到門外約 2 m 停好，按 Enter：")
        for label, where, optional in (("hold", "門檻線上（兩個鍋底貼合的平面）", False),
                                        ("far", "店內深處（離門口最遠、但仍收得到訊號的地方）", True)):
            ans = segment(f"to_{label}", f"  推到{where}停好，按 Enter 開始靜止 {HOLD_SEC:.0f} 秒"
                                         f"（s = {'不做' if optional else '跳過'}）：")
            if ans == "s":
                continue
            tap.label = label
            t0 = time.time()
            while time.time() - t0 < HOLD_SEC:
                left = HOLD_SEC - (time.time() - t0)
                print(f"\r    靜止中，剩 {left:4.1f} 秒，不要動...   ", end="", flush=True)
                time.sleep(0.2)
            print()
            marks.append(Mark(label, t0, time.time()))
    except KeyboardInterrupt:
        tap.stop()
        print("\n中斷。")
        return None
    tap.label = None
    tap.stop()
    recorder.write_meta(base, marks=[[m.label, m.t0, m.t1] for m in marks])
    print(f"  錄到 {len(tap.obs)} 筆：" + "、".join(f"{b} {n} 筆" for b, n in tap.counts.items()))
    return Session(sorted(tap.obs), marks)


# ======================================================================
# 錄音：5 個位置的靜止剖面
# ======================================================================
def collect(scanner, beacon_ids: List[str], seconds: float) -> List[Observation]:
    """收集 seconds 秒。開始前先清空佇列：走位、等 Enter 期間累積的封包不能算進這個位置。"""
    _drain(scanner.out_queue)
    t_start = time.time()
    t_end = t_start + seconds
    wanted = set(beacon_ids)
    out: List[Observation] = []
    counts = {b: 0 for b in beacon_ids}
    last_print = t_start
    while True:
        now = time.time()
        if now >= t_end:
            break
        try:
            obs = scanner.out_queue.get(timeout=0.1)
        except queue.Empty:
            obs = None
        if obs is not None and obs.beacon_id in wanted and obs.timestamp >= t_start:
            out.append((obs.timestamp, obs.beacon_id, float(obs.rssi), None))
            counts[obs.beacon_id] += 1
        if now - last_print >= 1.0:
            last_print = now
            c = "　".join(f"{b}={n}" for b, n in counts.items())
            print(f"\r  收集中... 剩 {t_end - now:4.1f}s　{c}   ", end="", flush=True)
    print()
    return out


def record_survey(scanner, base: GateParams, seconds: float, recorder: GateRecorder) -> Dict[str, List[Observation]]:
    ids = [base.inside_id, base.outside_id]
    segs: Dict[str, List[Observation]] = {}
    print("\n" + "=" * 84)
    print("靜止剖面：沿推車行進方向站 5 個位置，每個位置收集 "
          f"{seconds:.0f} 秒。Pi 的擺法要跟實際使用時一樣。")
    print("=" * 84)
    for key, label, hint, _ in POSITIONS:
        if input(f"  [{label}] {hint}，站好、人不要擋在中間，按 Enter（s = 跳過）：").strip().lower() == "s":
            continue
        obs = collect(scanner, ids, seconds)
        segs[key] = obs
        for t, bid, rssi, yaw in obs:
            recorder.record_observation(t, bid, rssi, yaw, None, segment=key)
    return segs


# ======================================================================
# 輸出
# ======================================================================
def print_calibration(cal: Calibration, base: GateParams) -> None:
    if cal.error:
        print(f"\n[無法校正] {cal.error}")
        return
    print("\n" + "=" * 84)
    print("每一組候選設定：遲滯從 2 到 12 dB 都試，整段錄音重播，"
          f"每一趟都判對、停在門口不亂跳、偏移量 ±{DRIFT_DB:g} dB 也成立的才算可行")
    has_logs = any(c.log_flaps is not None for c in cal.candidates)
    print(_pad("估計量", 8) + _pad("窗口", 8) + _pad("確認時間", 10) + _pad("可行遲滯範圍", 22)
          + (_pad("記錄來回跳", 12) if has_logs else "") + "不含漂移時")
    print("-" * 96)
    ranked = sorted(cal.candidates, key=lambda c: (-c.width, -c.best_partial[0]))
    for c in ranked[:12]:
        rng = "—" if c.interval is None else f"{c.interval[0]:.2f} ~ {c.interval[1]:.2f}（寬 {c.width:.2f}）"
        best = f"最多判對 {c.best_partial[0]}/{cal.n_passes} 趟" + (f"，{c.best_partial[2]}" if c.best_partial[2] else "")
        if c.flap_blocked:
            best += f"；{c.flap_blocked} 個遲滯值因記錄會來回跳被排除"
        cur = "  ← 現在" if (c.estimator, c.window, c.confirm_sec) == \
            (base.estimator, base.smoothing_sec, base.min_confirm_sec) else ""
        flap = (_pad("—" if c.log_flaps is None else f"{c.log_flaps} 次", 12) if has_logs else "")
        print(_pad(c.estimator, 8) + _pad(f"{c.window:g} 秒", 8) + _pad(f"{c.confirm_sec:g} 秒", 10)
              + _pad(rng, 22) + flap + best + cur)

    print("\n判讀：")
    if cal.verdict == "不夠":
        print(f"  ✗ 不夠。沒有任何一組設定能讓 {cal.n_passes} 趟全部判對又不亂跳（含 ±{DRIFT_DB:g} dB 漂移）。")
        print("    上表「不含漂移時」那一欄是各組最接近的結果與卡在哪裡。")
        print("    改善方向：(1) 先跑 --survey 看剖面：門外、門內哪一側的平台不夠寬")
        print("              (2) 兩顆的廣播間隔改 50 ms，同一個窗口裡樣本變多")
        print("              (3) 加強遮罩的前後差、線材不要繞到另一側、確認 Pi 擺法跟實際一樣")
        print("              (4) 執行期記錄會來回跳的話：Pi 的位置被車架或籃子擋住一側，先改擺法")
        _print_logs(cal, base)
        return
    p = cal.params
    c = cal.chosen
    mark = "✓ 可以" if cal.verdict == "可以" else "○ 勉強"
    print(f"  {mark}。可行遲滯範圍 {c.interval[0]:.2f} ~ {c.interval[1]:.2f} dB（寬 {c.width:.2f}），取中間。")

    print("\n用建議參數重播整段錄音（不含漂移）：")
    for i, (m, hit) in enumerate(zip([x for x in _passes(cal)], cal.check.pass_results), 1):
        dur = m.t1 - m.t0
        if hit is None:
            print(f"  ⚠ 第 {i} 趟{_KIND_NAME[m.kind]}：沒有判出來")
        else:
            print(f"  ✓ 第 {i} 趟{_KIND_NAME[m.kind]}：開始後 {hit:4.1f} 秒判出（這趟共 {dur:4.1f} 秒）")
    ev = cal.check.events
    for m in cal._session.quiet:  # noqa: SLF001
        n = sum(1 for t, _ in ev if m.t0 <= t <= m.t1)
        where = "門檻線" if m.label == "hold" else "店內深處"
        print(f"  {'✓' if n <= 1 else '⚠'} 停在{where} {m.t1 - m.t0:.0f} 秒：{n} 次事件（最多容許 1 次）")

    _print_logs(cal, base)

    def tag(new, old):
        return "（不變）" if new == old else f"（原本 {old}）"

    print("\n建議參數：")
    print(f"  landmarks.rssi_estimator                       = {p.estimator}{tag(p.estimator, base.estimator)}")
    print(f"  landmarks.rssi_smoothing_sec                   = {p.smoothing_sec:.1f}"
          f"{tag(p.smoothing_sec, base.smoothing_sec)}")
    print(f"  landmarks.gate_min_confirm_sec                 = {p.min_confirm_sec:g}"
          f"{tag(p.min_confirm_sec, base.min_confirm_sec)}")
    print(f"  landmarks.gate_min_confirm_samples             = {p.min_confirm_samples}（不變，不調）")
    print(f"  landmarks.points[{p.inside_id}].rssi_offset_db  = {p.offset_inside:+.1f}")
    print(f"  landmarks.points[{p.outside_id}].rssi_offset_db = 0.0（固定當基準）")
    print(f"  landmarks.gate_hysteresis_dbm                  = {p.hysteresis_dbm:.2f}")
    print(f"  landmarks.gate_min_crossing_rssi_dbm           = {p.min_crossing_rssi_dbm:.1f}")


def _fmt_events(events, t0: float) -> str:
    return "、".join(f"{t - t0:.1f}s {'進' if e == 'entering' else '出'}" for t, e in events) or "（沒有事件）"


def _print_logs(cal: Calibration, base: GateParams) -> None:
    """執行期記錄重播的結果：現在的設定 vs 建議的設定，各來回跳幾次。"""
    logs = getattr(cal, "_logs", None)
    if not logs:
        return
    print(f"\n執行期記錄重播（{len(logs)} 份，相反的事件間隔不到 {FLAP_SEC:g} 秒算來回跳）：")
    for i, (name, obs) in enumerate(logs):
        t0 = obs[0][0] if obs else 0.0
        b = cal.base_log_events[i] if cal.base_log_events else []
        print(f"  {name}（{(obs[-1][0] - t0) / 60:.1f} 分鐘）")
        print(f"    現在的設定：來回跳 {len(flaps(b))} 次　{_fmt_events(b, t0)}")
        if cal.log_events:
            n = cal.log_events[i]
            print(f"    建議的設定：來回跳 {len(flaps(n))} 次　{_fmt_events(n, t0)}")


def _passes(cal: Calibration):
    return cal._session.passes  # noqa: SLF001


def print_survey(segs: Dict[str, List[Observation]], base: GateParams) -> None:
    stats = {k: segment_stats(v, base) for k, v in segs.items() if k in LABELS}
    print("\n" + "=" * 84)
    print(f"靜止剖面（現在的設定：{base.estimator} / {base.smoothing_sec:g} 秒 / 門內偏移 {base.offset_inside:+.1f}；"
          "濾波後中位數）")
    print(_pad("位置", 14) + _pad("內側", 9) + _pad("外側", 9) + _pad("差分 內−外", 13)
          + _pad("σ", 7) + "包/秒（內/外）")
    print("-" * 84)
    for key, label, _, _ in POSITIONS:
        if key not in stats:
            continue
        s = stats[key]
        if s is None:
            print(_pad(label, 14) + "資料不足")
            continue
        print(_pad(label, 14) + _pad(f"{s.inside:7.1f}", 9) + _pad(f"{s.outside:7.1f}", 9)
              + _pad(f"{s.delta:+7.1f} dB", 13) + _pad(f"{s.noise:4.1f}", 7) + f"{s.rate_in:.1f}/{s.rate_out:.1f}")
    notes = survey_notes(stats, base)
    print("\n" + ("要注意的地方：" if notes else "形狀看起來沒有明顯問題。"))
    for n in notes:
        print(f"  - {n}")
    print("\n（這只看形狀、不挑參數。參數用走過門口的校正錄音來定。）")


# ======================================================================
# 寫回
# ======================================================================
def write_back(p: GateParams, source: str) -> None:
    """只改 landmarks 區塊裡的這幾個值，其他欄位與 _comment 註解都留著。"""
    cfg = config.load()
    lm = cfg.setdefault("landmarks", {})
    for point in lm.get("points", []):
        if point["beacon_id"] in p.offsets:
            point["rssi_offset_db"] = round(p.offsets[point["beacon_id"]], 1)
    lm["rssi_estimator"] = p.estimator
    lm["rssi_smoothing_sec"] = p.smoothing_sec
    lm["gate_min_confirm_samples"] = p.min_confirm_samples
    lm["gate_min_confirm_sec"] = p.min_confirm_sec
    lm["_gate_min_confirm_sec_comment"] = (
        "門口判定的確認持續時間（秒）：新的一側除了連續 gate_min_confirm_samples 筆，還要從第一筆算起持續這麼久"
        "才算數（core/gate/detector.GateCrossingDetector 第 4 點）。光數筆數不夠：每秒約 13 筆，3 筆只有 0.2 秒，"
        "而平滑值可能被單一一筆強封包撐住一整個窗口。由 tools/calibrate_gate_beacons.py 挑，0 = 不看時間（舊版行為）。")
    lm["gate_hysteresis_dbm"] = round(p.hysteresis_dbm, 2)
    lm["gate_min_crossing_rssi_dbm"] = round(p.min_crossing_rssi_dbm, 1)
    lm["gate_calibration_source"] = source
    lm["_gate_calibration_source_comment"] = (
        "最後一次寫回門口參數的校正錄音（tools/calibrate_gate_beacons.py）。"
        "用 --from-data 加這個路徑可以重算、核對。"
    )
    config.save(cfg)
    print(f"\n已寫回 {config.rel(config.CONFIG_PATH)}")


def save_result(out_dir: Path, cal: Calibration) -> None:
    result = {
        "verdict": cal.verdict,
        "error": cal.error,
        "params": None if cal.params is None else cal.params.to_dict(),
        "candidates": [
            {"estimator": c.estimator, "window": c.window, "confirm_sec": c.confirm_sec, "log_flaps": c.log_flaps,
             "flap_blocked": c.flap_blocked, "offset": c.offset,
             "min_crossing": c.min_crossing, "interval": c.interval, "best_partial": c.best_partial, "info": c.info}
            for c in cal.candidates
        ],
        "check": None if cal.check is None else {
            "passed": cal.check.passed, "reason": cal.check.reason,
            "pass_results": cal.check.pass_results, "events": cal.check.events},
    }
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def runtime_logs(since_epoch: float) -> List[tuple]:
    """data/gate_logs/ 裡、第一筆觀測在 since_epoch 之後的執行期記錄。回傳 [(名稱, 觀測)]。"""
    out = []
    for path in sorted(RUNTIME_LOG_ROOT.glob("*/*.csv")):
        try:
            segs, _, meta = load(path)
        except Exception:  # noqa: BLE001 — 壞掉的記錄檔跳過就好
            continue
        obs = segs.get("run", [])
        if len(obs) < 20 or obs[0][0] < since_epoch:
            continue
        out.append((config.rel(path), obs))
    return out


def run_calibration(session: Session, base: GateParams, logs: Optional[List[tuple]] = None) -> Calibration:
    logs = logs or []
    if logs:
        mins = sum((o[-1][0] - o[0][0]) for _, o in logs) / 60
        print(f"  一起重播 {len(logs)} 份執行期記錄（共 {mins:.1f} 分鐘），排除會來回跳的參數")

    def progress(done, total):
        print(f"\r  計算中 {done}/{total}（每一組設定都把整段錄音重播很多次）...", end="", flush=True)
    cal = calibrate_session(session, base, logs=[o for _, o in logs], progress=progress)
    print()
    cal._session = session  # noqa: SLF001 — 輸出逐趟結果用
    cal._logs = logs        # noqa: SLF001
    return cal


def finish(cal: Calibration, base: GateParams, dry_run: bool, source: str) -> int:
    print_calibration(cal, base)
    if cal.params is None:
        return 1
    if dry_run:
        print("\n--dry-run：沒有寫回 config.json。")
        return 0
    if not cal.check.passed:
        print("\n建議參數的重播沒有全部通過，不寫回。")
        return 1
    if input("\n要寫回 config.json 嗎？(y 確認，其他任何輸入都不寫)：").strip().lower() != "y":
        print("沒有寫回。")
        return 0
    write_back(cal.params, source)
    print("接下來：開 app 或 --monitor 實際推車進出幾次，再跑 python3 -m tools.analyze_gate_log 檢查。")
    return 0


# ======================================================================
def run_monitor(scanner, gate_monitor, refresh_sec: float = 0.25) -> None:
    """即時監看：原始讀數、濾波後（GateMonitor 實際用來判定）的值、判定結果並排顯示。

    用的就是 GateMonitor 本身（跟 ui/app_gui.py 走同一條路徑），而且跟執行期一樣
    會把每一筆值記錄到 data/gate_logs/，事後可以用 tools/analyze_gate_log.py 分析。
    """
    print("\n" + "=" * 78)
    print("即時監看（Ctrl+C 結束）")
    print("=" * 78)
    print(gate_monitor.describe())
    print("\n原始 = 最近一筆未處理的讀數　濾波 = 已加偏移、GateMonitor 實際拿去判定的值")
    print("Δ = 濾波後 內側 − 外側，超過 +遲滯 偏門內、低於 −遲滯 偏門外\n")

    raw = {gate_monitor.inside_beacon_id: None, gate_monitor.outside_beacon_id: None}
    last_print = 0.0
    events = []

    def fmt(v, unit=""):
        return f"{v:>6.1f}{unit}" if v is not None else "   --  "

    try:
        while True:
            try:
                obs = scanner.out_queue.get(timeout=0.2)
            except queue.Empty:
                obs = None
            if obs is not None:
                if obs.beacon_id in raw:
                    raw[obs.beacon_id] = obs.rssi
                crossing = gate_monitor.process_observation(obs.beacon_id, obs.rssi, obs.timestamp)
                if crossing:
                    events.append(crossing)
                    print(f"\n  *** 判定：{crossing} ***  (累計 {len(events)} 次)\n")
            now = time.time()
            if now - last_print < refresh_sec:
                continue
            last_print = now
            snap = gate_monitor.snapshot()
            if snap["diff"] is None:
                status = f"樣本不足（內 {snap['n_inside']} / 外 {snap['n_outside']}）"
            else:
                abs_ok = snap["above_abs_threshold"]
                status = f"判定={_SIDE_NAME[snap['dominant']]}" + \
                    ("" if abs_ok in (None, True) else "  [訊號太弱，絕對門檻擋下]")
            print(f"\r  原始 內{fmt(raw[gate_monitor.inside_beacon_id])} 外{fmt(raw[gate_monitor.outside_beacon_id])} │ "
                  f"濾波 內{fmt(snap['inside'])} 外{fmt(snap['outside'])} │ "
                  f"Δ={fmt(snap['diff'], ' dB')} │ {status}        ", end="", flush=True)
    except KeyboardInterrupt:
        print(f"\n\n結束。這段期間共判定出 {len(events)} 次穿越：{events or '（沒有）'}")


def _start_scanner(base: GateParams, adapter: Optional[str]):
    # 啟動 + 等兩顆 Beacon 最多要 30 秒，這段期間一定要有狀態輸出，不然看起來像卡住。
    print(f"啟動藍牙掃描，等待 {base.inside_id} 與 {base.outside_id} 都出現（最多 20 秒）...", flush=True)
    scanner = None
    try:
        scanner = BleBeaconScanner(beacon_ids=load_beacon_ids(), address_map=load_beacon_identity_map(),
                                   out_queue=queue.Queue(), adapter=adapter)
        scanner.start()
        scanner.wait_for_beacons([base.inside_id, base.outside_id], timeout_sec=20.0)
    except KeyboardInterrupt:
        if scanner is not None:
            scanner.stop()
        print("\n中斷。")
        return None
    except Exception as exc:  # noqa: BLE001
        if scanner is not None:
            scanner.stop()
        print(f"\n[錯誤] BLE 掃描器啟動失敗：{exc}")
        print("  1) sudo rfkill unblock bluetooth　2) pip install bleak")
        print("  3) 查實際位址/名稱：python3 -m drivers.ble_beacon_scanner --list")
        print("  這支需要門口兩顆 Beacon 同時有電。\n")
        return None
    print("兩顆都掃到了。", flush=True)
    return scanner


def _parse_since(text: str) -> float:
    return datetime.strptime(text, "%Y%m%d-%H%M%S").timestamp()


def _from_data(path: Path, base: GateParams, dry_run: bool, use_logs: bool = True,
               logs_since: Optional[str] = None) -> int:
    csv_path = path / "observations.csv" if path.is_dir() else path
    if not csv_path.exists():
        print(f"[錯誤] 找不到 {csv_path}")
        return 1
    segs, _, meta = load(csv_path)
    if not any(k.startswith(("enter", "exit")) for k in segs):
        segs = {k: v for k, v in segs.items() if k in LABELS}
        print(f"讀入 {config.rel(csv_path)}（靜止剖面）")
        print_survey(segs, base)
        return 0
    session = Session.from_recording(segs, meta.get("marks"))
    print(f"讀入 {config.rel(csv_path)}：{len(session.obs)} 筆、{len(session.passes)} 趟")
    logs = []
    if use_logs:
        since = _parse_since(logs_since) if logs_since else session.obs[0][0]
        logs = runtime_logs(since)
        if not logs:
            print("  （沒有校正錄音之後的執行期記錄；實際推車用一陣子再重算，可以多排除會來回跳的參數）")
    cal = run_calibration(session, base, logs)
    save_result(csv_path.parent, cal)
    return finish(cal, base, dry_run, config.rel(csv_path.parent))


def _main() -> int:
    ap = argparse.ArgumentParser(description="管制區門口 Beacon 校正")
    ap.add_argument("--dry-run", action="store_true", help="錄音、挑參數，不寫回 config.json")
    ap.add_argument("--trips", type=int, default=4, help="來回幾次（預設 4，也就是 8 趟）")
    ap.add_argument("--survey", action="store_true", help="5 個位置的靜止剖面（不挑參數）")
    ap.add_argument("--seconds", type=float, default=15.0, help="--survey 每個位置收集幾秒，預設 15")
    ap.add_argument("--from-data", default=None, help="用存下來的錄音重算（目錄或 observations.csv）")
    ap.add_argument("--monitor", action="store_true", help="即時監看原始值、濾波值與判定")
    ap.add_argument("--adapter", default=None, help="藍牙介面名稱，預設用系統預設")
    ap.add_argument("--no-logs", action="store_true", help="--from-data 時不用執行期記錄檢查來回跳")
    ap.add_argument("--logs-since", default=None,
                    help="--from-data 時用這個時間之後的執行期記錄（YYYYmmdd-HHMMSS，預設 = 校正錄音開始時間）")
    args = ap.parse_args()

    cfg = load_landmark_config()
    base = GateParams.from_landmark_config(cfg)
    if not base.inside_id or not base.outside_id:
        print("[錯誤] config.json 的 landmarks.gate_beacon_pair 沒有設定 inside_id / outside_id")
        return 1
    if args.from_data:
        return _from_data(Path(args.from_data), base, args.dry_run, not args.no_logs, args.logs_since)

    scanner = _start_scanner(base, args.adapter)
    if scanner is None:
        return 1
    try:
        if args.monitor:
            from core.gate.monitor import GateMonitor
            monitor = GateMonitor.from_config(cfg, recorder=GateRecorder.for_runtime("monitor"))
            try:
                run_monitor(scanner, monitor)
            finally:
                monitor.close()
            return 0

        stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
        if args.survey:
            out_dir = _SURVEY_ROOT / stamp
            rec = GateRecorder(out_dir / "observations.csv", kind="survey",
                               extra_meta={"seconds_per_position": args.seconds})
            rec.start(base)
            segs = record_survey(scanner, base, args.seconds, rec)
            rec.close()
            print_survey(segs, base)
            print(f"\n資料存在 {config.rel(out_dir)}/")
            return 0

        out_dir = _CAL_ROOT / stamp
        rec = GateRecorder(out_dir / "observations.csv", kind="calibration_walk", extra_meta={"trips": args.trips})
        rec.start(base)
        session = record_walkthrough(scanner, base, args.trips, rec)
        rec.close()
        if session is None:
            print(f"已錄到的部分存在 {config.rel(out_dir)}/，沒有寫回任何東西。")
            return 1
        scanner.stop()
        cal = run_calibration(session, base)
        save_result(out_dir, cal)
        print(f"錄音與結果：{config.rel(out_dir)}/")
        return finish(cal, base, args.dry_run, config.rel(out_dir))
    except KeyboardInterrupt:
        print("\n中斷，沒有寫回任何東西。")
        return 1
    finally:
        scanner.stop()


if __name__ == "__main__":
    sys.exit(_main())
