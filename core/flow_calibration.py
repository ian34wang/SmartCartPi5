"""
core/flow_calibration.py

PMW3901 光流與 BNO080 航向的校正計算（tools/calibrate_optical_flow.py 負責錄音與
互動）。跟秤重、門口同一個原則：算出參數之後，把錄音**丟進執行期同一份程式
（core/odometry_engine.OdometryEngine）重播**，看推出來的距離與方向對不對。

錄音的段落（label）：
    static        推車停著不動            → 靜止時光流有沒有亂跳、yaw 漂移多快
    fwd_1, fwd_2… 沿直線往前推一段量好的距離 D（meta: distance_mm）
    left_1        往車子左側橫推（選做）   → 判斷感測器有沒有左右顛倒
    turn_left     原地向左（逆時針）轉 90°  → yaw 的正負號

符號：
    (dx, dy)   PMW3901 每筆回報的位移（count，感測器自己的軸）
    S = (Σdx, Σdy)   一趟的累積位移向量；|S| 是長度
    D          這一趟實際距離（mm）
    k          optical_flow_px_to_mm = D / |S|（mm/count）
    θ          atan2(Σdy, Σdx)：一趟在感測器軸上的方向角
    m          鏡像：+1 或 −1（odometry.flow_mirror）
    φ          安裝角（odometry.flow_rotation_deg）= 直推方向在「去鏡像後」感測器軸上的角度
    yaw_sign   +1 或 −1（odometry.yaw_sign）：原地左轉時 yaw 增加為 +1

計算：
    1. k：各趟直推 D/|S| 的平均；變異係數 CV = 標準差/平均，看推得穩不穩
    2. m：側推方向 θ_l 跟直推方向 θ_f 差 δ = θ_l − θ_f；δ ≈ +90° → m = +1，δ ≈ −90° → m = −1
       （從下往上看地板時軸會左右顛倒，就是 −90°）。沒做側推就假設 m = +1
    3. φ = atan2(m·S̄_y, S̄_x)，S̄ 是各趟直推的單位方向向量平均
    4. yaw_sign：原地左轉前後 yaw 的變化（接成連續角度後）是正的 → +1
    5. 重播：用 1~4 的參數建立 OdometryEngine，把每一趟原始封包餵進去，比較推出來的
       位移長度與方向（直推應該沿起點航向、側推應該在起點航向 +90°）

k 用的是**沒有濾掉離群值**的累加：執行期的 OdometryEngine 不濾離群值，校正也不濾，
兩邊才一致。離群值有幾筆會列出來，太多代表感測器高度或地板反光有問題。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from core.odometry_engine import OdometryEngine
from core.uart_recording import Recording, Sample, unwrap_deg, wrap_deg

GOOD_CV = 0.03
GOOD_SPREAD_DEG = 5.0
GOOD_REPLAY_PCT = 3.0
TURN_DEG = 90.0
MAD_K = 6.0


@dataclass
class RunResult:
    label: str
    distance_mm: Optional[float]
    n: int
    outliers: int
    sum_dx: float
    sum_dy: float
    counts: float               # |S|
    k: Optional[float]          # D/|S|
    theta_deg: float            # 感測器軸上的方向
    yaw_change_deg: float       # 這一趟 yaw 變了多少（直推應該接近 0）
    squal_median: float
    squal_p5: float
    replay_dist_mm: Optional[float] = None
    replay_err_pct: Optional[float] = None
    replay_heading_err_deg: Optional[float] = None


@dataclass
class FlowCalibration:
    error: Optional[str]
    verdict: str
    px_to_mm: Optional[float] = None
    cv: Optional[float] = None
    rotation_deg: Optional[float] = None
    theta_spread_deg: Optional[float] = None
    mirror: Optional[bool] = None
    lateral_delta_deg: Optional[float] = None
    yaw_sign: Optional[int] = None
    turn_measured_deg: Optional[float] = None
    static_flow_per_sec: Optional[float] = None
    static_yaw_drift_deg_per_min: Optional[float] = None
    runs: List[RunResult] = field(default_factory=list)
    lateral: Optional[RunResult] = None
    issues: List[str] = field(default_factory=list)


# ----------------------------------------------------------------------
def count_outliers(samples: Sequence[Sample], k: float = MAD_K) -> int:
    mags = [math.hypot(s.dx, s.dy) for s in samples]
    if len(mags) < 5:
        return 0
    med = statistics.median(mags)
    mad = statistics.median(abs(m - med) for m in mags)
    if mad == 0:
        return 0
    return sum(1 for m in mags if m > med + k * mad)


def run_result(label: str, samples: Sequence[Sample], distance_mm: Optional[float]) -> RunResult:
    sx = float(sum(s.dx for s in samples))
    sy = float(sum(s.dy for s in samples))
    counts = math.hypot(sx, sy)
    yaws = unwrap_deg([s.yaw_deg for s in samples])
    sq = sorted(s.squal for s in samples)
    return RunResult(
        label=label, distance_mm=distance_mm, n=len(samples), outliers=count_outliers(samples),
        sum_dx=sx, sum_dy=sy, counts=counts,
        k=(distance_mm / counts) if distance_mm and counts > 0 else None,
        theta_deg=math.degrees(math.atan2(sy, sx)),
        yaw_change_deg=(yaws[-1] - yaws[0]) if yaws else 0.0,
        squal_median=statistics.median(sq) if sq else 0.0,
        squal_p5=sq[int(0.05 * (len(sq) - 1))] if sq else 0.0,
    )


def _edge_median(values: List[float], times: List[float], first: bool, span: float = 0.5) -> float:
    if first:
        sel = [v for v, t in zip(values, times) if t <= times[0] + span]
    else:
        sel = [v for v, t in zip(values, times) if t >= times[-1] - span]
    return statistics.median(sel or values)


def turn_angle(samples: Sequence[Sample]) -> Optional[float]:
    if len(samples) < 5:
        return None
    yaws = unwrap_deg([s.yaw_deg for s in samples])
    ts = [s.t for s in samples]
    return _edge_median(yaws, ts, False) - _edge_median(yaws, ts, True)


def yaw_drift_per_min(samples: Sequence[Sample]) -> Optional[float]:
    if len(samples) < 10:
        return None
    yaws = unwrap_deg([s.yaw_deg for s in samples])
    ts = [s.t - samples[0].t for s in samples]
    t_bar, y_bar = statistics.fmean(ts), statistics.fmean(yaws)
    sxx = sum((t - t_bar) ** 2 for t in ts)
    if sxx == 0:
        return None
    return sum((t - t_bar) * (y - y_bar) for t, y in zip(ts, yaws)) / sxx * 60.0


def _packets(samples: Sequence[Sample]):
    from drivers.uart_receiver import UartPacket   # 延後載入：pyserial 只在實機上需要
    return [UartPacket(s.dx, s.dy, s.squal, s.yaw_deg, s.pitch_deg, s.roll_deg, s.hx711_raw, s.t) for s in samples]


def replay(samples: Sequence[Sample], k: float, rotation_deg: float, mirror: bool, yaw_sign: int,
           expected_offset_deg: float) -> Tuple[float, float]:
    """用 OdometryEngine 重播一趟，回傳 (位移長度 mm, 方向誤差 deg)。

    期望方向 = 起點航向 + expected_offset_deg（直推 0°、側推 +90°）。
    """
    eng = OdometryEngine(px_to_mm=k, rotation_deg=rotation_deg, mirror=mirror, yaw_sign=yaw_sign)
    pk = _packets(samples)
    eng.process_packet(type(pk[0])(0, 0, pk[0].squal, pk[0].yaw_deg, 0.0, 0.0, 0, pk[0].timestamp))
    start = eng.get_state()
    for p in pk:
        eng.process_packet(p)
    end = eng.get_state()
    dx, dy = end.x_mm - start.x_mm, end.y_mm - start.y_mm
    heading = math.degrees(math.atan2(dy, dx))
    return math.hypot(dx, dy), wrap_deg(heading - (start.yaw_deg + expected_offset_deg))


def _circ_mean(angles_deg: Sequence[float]) -> Tuple[float, float]:
    """回傳 (平均角, 最大偏離)。"""
    c = statistics.fmean(math.cos(math.radians(a)) for a in angles_deg)
    s = statistics.fmean(math.sin(math.radians(a)) for a in angles_deg)
    mean = math.degrees(math.atan2(s, c))
    return mean, max(abs(wrap_deg(a - mean)) for a in angles_deg)


# ----------------------------------------------------------------------
def calibrate(rec: Recording) -> FlowCalibration:
    fwd_segs = [s for s in rec.segs("fwd") if len(rec.get(s.label)) >= 5]
    if not fwd_segs:
        return FlowCalibration("沒有直推的資料（fwd_1…）。", "錯誤")
    runs = [run_result(s.label, rec.get(s.label), float(s.info.get("distance_mm", 0)) or None) for s in fwd_segs]
    good = [r for r in runs if r.k is not None]
    if not good:
        return FlowCalibration("直推時感測器完全沒有讀到位移——檢查 PMW3901 離地高度、鏡頭有沒有被擋住。", "錯誤",
                               runs=runs)
    cal = FlowCalibration(None, "", runs=runs)
    ks = [r.k for r in good]
    cal.px_to_mm = statistics.fmean(ks)
    cal.cv = statistics.pstdev(ks) / cal.px_to_mm if len(ks) > 1 else None
    theta_f, cal.theta_spread_deg = _circ_mean([r.theta_deg for r in good])

    # 鏡像
    lat = [s for s in rec.segs("left") if len(rec.get(s.label)) >= 5]
    m = 1
    if lat:
        cal.lateral = run_result(lat[0].label, rec.get(lat[0].label), float(lat[0].info.get("distance_mm", 0)) or None)
        if cal.lateral.counts > 0:
            cal.lateral_delta_deg = wrap_deg(cal.lateral.theta_deg - theta_f)
            if 60 <= cal.lateral_delta_deg <= 120:
                cal.mirror, m = False, 1
            elif -120 <= cal.lateral_delta_deg <= -60:
                cal.mirror, m = True, -1
            else:
                cal.issues.append(f"側推方向跟直推差 {cal.lateral_delta_deg:+.0f}°（應該接近 ±90°），判斷不出鏡像"
                                  "——側推時車子可能有在轉，或推的方向不是正左方。")
        else:
            cal.issues.append("側推時感測器沒有讀到位移（輪子可能沒有真的橫著走）。")
    else:
        cal.issues.append("沒有做側推，假設感測器沒有左右顛倒（flow_mirror = false）。")
    mean_x = statistics.fmean(math.cos(math.radians(r.theta_deg)) for r in good)
    mean_y = statistics.fmean(math.sin(math.radians(r.theta_deg)) for r in good)
    cal.rotation_deg = math.degrees(math.atan2(m * mean_y, mean_x))

    # 航向
    turn = rec.segs("turn")
    if turn:
        cal.turn_measured_deg = turn_angle(rec.get(turn[0].label))
    if cal.turn_measured_deg is None:
        cal.issues.append("沒有原地左轉的資料，yaw 正負號沒有驗證（假設逆時針為正）。")
    elif abs(cal.turn_measured_deg) < 45:
        cal.issues.append(f"原地左轉時 yaw 只變了 {cal.turn_measured_deg:+.1f}°——車子有轉嗎？BNO080 的資料有在更新嗎？")
    else:
        cal.yaw_sign = 1 if cal.turn_measured_deg > 0 else -1
        err = abs(cal.turn_measured_deg) - TURN_DEG
        if abs(err) > 10:
            cal.issues.append(f"左轉 90° 量到 {abs(cal.turn_measured_deg):.1f}°（差 {err:+.1f}°）——對齊地板線再轉一次確認，"
                              "差這麼多可能是 BNO080 還沒穩定或轉的角度不準。")
    ys = cal.yaw_sign or 1

    st = rec.get("static")
    if len(st) >= 10:
        dur = st[-1].t - st[0].t
        cal.static_flow_per_sec = sum(abs(s.dx) + abs(s.dy) for s in st) / dur if dur > 0 else None
        cal.static_yaw_drift_deg_per_min = yaw_drift_per_min(st)

    # 用 OdometryEngine 重播
    for r, s in zip(runs, fwd_segs):
        if r.distance_mm:
            dist, herr = replay(rec.get(s.label), cal.px_to_mm, cal.rotation_deg, bool(cal.mirror), ys, 0.0)
            r.replay_dist_mm, r.replay_heading_err_deg = dist, herr
            r.replay_err_pct = (dist - r.distance_mm) / r.distance_mm * 100
    if cal.lateral is not None and cal.mirror is not None:
        dist, herr = replay(rec.get(lat[0].label), cal.px_to_mm, cal.rotation_deg, cal.mirror, ys, 90.0)
        cal.lateral.replay_dist_mm, cal.lateral.replay_heading_err_deg = dist, herr

    # 判讀
    if len(good) < 2:
        cal.issues.append("只推了一趟，看不出重複性。")
    elif cal.cv is not None and cal.cv > GOOD_CV:
        cal.issues.append(f"各趟的 k 差異 {cal.cv:.1%}（> {GOOD_CV:.0%}）——推的路線不夠直、距離量得不準，"
                          "或某一趟感測器掉追蹤（看 squal）。")
    if cal.theta_spread_deg > GOOD_SPREAD_DEG:
        cal.issues.append(f"各趟直推的方向差到 {cal.theta_spread_deg:.1f}°——推的時候車頭有偏。")
    for r in runs:
        if r.outliers:
            cal.issues.append(f"{r.label} 有 {r.outliers} 筆位移異常大（執行期不會濾掉，會直接算進去）。")
        if abs(r.yaw_change_deg) > 10:
            cal.issues.append(f"{r.label} 推的過程 yaw 變了 {r.yaw_change_deg:+.1f}°，不是直線。")
        if r.replay_err_pct is not None and abs(r.replay_err_pct) > GOOD_REPLAY_PCT:
            cal.issues.append(f"{r.label} 重播距離誤差 {r.replay_err_pct:+.1f}%。")
    if cal.static_flow_per_sec is not None and cal.static_flow_per_sec > 5:
        cal.issues.append(f"推車沒動時光流每秒還累積 {cal.static_flow_per_sec:.0f} count——離地高度或光線有問題，"
                          "停著也會慢慢「走」。")
    ok = (len(good) >= 2 and cal.mirror is not None and cal.yaw_sign is not None and
          (cal.cv or 0) <= GOOD_CV and cal.theta_spread_deg <= GOOD_SPREAD_DEG and
          all(r.replay_err_pct is None or abs(r.replay_err_pct) <= GOOD_REPLAY_PCT for r in runs))
    cal.verdict = "可以" if ok and not cal.issues else "勉強"
    return cal
