"""
core/positioning/track.py

里程計（光流 + IMU）行駛軌跡：把一段 UART 封包照 app 的算法（core/positioning/odometry.OdometryEngine，
參數是 config.json 的 odometry 區塊）重播成軌跡、算誤差、畫成圖。tools/track_odometry.py 用
（即時推車、事後重播都是這一套）。純邏輯，不碰硬體。

符號：
    (X_i, Y_i)   第 i 筆封包之後的里程計全域位置（mm；原點與方向是 OdometryEngine 的全域座標）
    ψ_i          第 i 筆的里程計航向（度，逆時針為正 = 左轉為正）
    i = 0 … N    這一趟的第一筆到最後一筆

起點座標（畫圖、比對都用這個）：原點是起點、+f 軸是起點時的車頭方向、+l 是起點時的左方
    [f_i]   [ cos ψ₀   sin ψ₀] [X_i − X₀]
    [l_i] = [−sin ψ₀   cos ψ₀] [Y_i − Y₀]
所以不管開機時車子朝哪裡，圖上起點一律朝上。

指標：
    L   = Σ_{i=1..N} ‖(X_i, Y_i) − (X_{i−1}, Y_{i−1})‖        路徑長
    D   = ‖(f_N, l_N)‖                                        起點到終點的直線距離
    Δψ  = unwrap(ψ_N − ψ₀)                                    總轉角（左轉為正，可以超過 ±180°）
    有捲尺量的終點 (f*, l*)（起點座標）：
        e = (f_N − f*, l_N − l*)，‖e‖ 終點誤差，‖e‖ / L 每走一公尺差多少
        繞一圈回起點時 (f*, l*) = (0, 0)，‖e‖ 就是閉合誤差
    有實際直線距離 d*：e_D = D − d*
    有實際轉角 Δψ*：e_ψ = Δψ − Δψ*

重播細節：OdometryEngine 用「上一筆」的航向轉這一筆的位移；新開的引擎航向是 0，所以先餵一筆
dx = dy = 0、航向 = 第一筆航向的封包，讓第一筆位移也用對的航向（app 裡引擎從開機一直在跑，沒有這問題）。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from core.angles import wrap_deg
from core.positioning.odometry import OdometryEngine

# 封包間隔超過這麼久算掉資料（UART 20 Hz，正常間隔 0.05 s）
GAP_SEC = 0.15


@dataclass(frozen=True)
class _Pkt:
    """OdometryEngine.process_packet 需要的欄位（core/recording.Sample 的 t 改名 timestamp）。"""
    dx: int
    dy: int
    squal: int
    yaw_deg: float
    timestamp: float


@dataclass
class TrackPoint:
    t: float          # 封包時間（epoch 秒）
    x_mm: float       # 里程計全域 X
    y_mm: float       # 里程計全域 Y
    yaw_deg: float    # 里程計航向 ψ
    squal: int


@dataclass
class Track:
    """一趟軌跡。points[0] 是起點（餵完第一筆之前的位置）。"""
    points: List[TrackPoint] = field(default_factory=list)
    skipped: int = 0      # squal < min_squal 沒有累加位置的筆數

    def start_frame(self) -> List[Tuple[float, float]]:
        """每一點在起點座標的 (f, l)，公尺。"""
        if not self.points:
            return []
        p0 = self.points[0]
        a = math.radians(p0.yaw_deg)
        c, s = math.cos(a), math.sin(a)
        out = []
        for p in self.points:
            dx, dy = (p.x_mm - p0.x_mm) / 1000.0, (p.y_mm - p0.y_mm) / 1000.0
            out.append((c * dx + s * dy, -s * dx + c * dy))
        return out


class TrackBuilder:
    """一筆一筆餵封包（即時）或整段餵（重播），算法就是 app 的 OdometryEngine。"""

    def __init__(self, odometry_cfg: dict, min_squal: int = 0):
        self.engine = OdometryEngine.from_config(odometry_cfg, min_squal=min_squal)
        self.track = Track()
        self._primed = False

    def feed(self, s) -> None:
        """s：core/recording.Sample（或任何有 t, dx, dy, squal, yaw_deg 的東西）。"""
        if not self._primed:
            self.engine.process_packet(_Pkt(0, 0, 255, s.yaw_deg, s.t))
            st = self.engine.get_state()
            self.track.points.append(TrackPoint(s.t, st.x_mm, st.y_mm, st.yaw_deg, s.squal))
            self._primed = True
        st = self.engine.process_packet(_Pkt(s.dx, s.dy, s.squal, s.yaw_deg, s.t))
        self.track.points.append(TrackPoint(s.t, st.x_mm, st.y_mm, st.yaw_deg, s.squal))
        self.track.skipped = st.skipped_low_confidence_count

    def feed_all(self, samples: Iterable) -> Track:
        for s in samples:
            self.feed(s)
        return self.track


def build_track(samples: Sequence, odometry_cfg: dict, min_squal: int = 0) -> Track:
    return TrackBuilder(odometry_cfg, min_squal).feed_all(samples)


def metrics(track: Track, true_f_m: Optional[float] = None, true_l_m: Optional[float] = None,
            true_distance_m: Optional[float] = None, true_turn_deg: Optional[float] = None) -> Dict:
    """指標（定義見檔案開頭）。沒給的實際值，對應的誤差就是 None（不補值）。"""
    pts = track.points
    out: Dict = {"samples": max(0, len(pts) - 1), "skipped_low_squal": track.skipped}
    if len(pts) < 2:
        return out
    sf = track.start_frame()
    L = sum(math.hypot(b.x_mm - a.x_mm, b.y_mm - a.y_mm) for a, b in zip(pts, pts[1:])) / 1000.0
    f_end, l_end = sf[-1]
    turn = 0.0
    for a, b in zip(pts, pts[1:]):
        turn += wrap_deg(b.yaw_deg - a.yaw_deg)
    times = [p.t for p in pts[1:]]
    gaps = [b - a for a, b in zip(times, times[1:])]
    squals = sorted(p.squal for p in pts[1:])
    out.update({
        "duration_s": times[-1] - times[0] if len(times) > 1 else 0.0,
        "path_m": L,
        "end_f_m": f_end,
        "end_l_m": l_end,
        "displacement_m": math.hypot(f_end, l_end),
        "turn_deg": turn,
        "gaps": sum(1 for g in gaps if g > GAP_SEC),
        "max_gap_s": max(gaps) if gaps else 0.0,
        "squal_median": statistics.median(squals),
        "squal_p5": squals[int(0.05 * (len(squals) - 1))],
        "end_err_m": None, "end_err_f_m": None, "end_err_l_m": None, "end_err_pct_of_path": None,
        "distance_err_m": None, "turn_err_deg": None,
    })
    if true_f_m is not None and true_l_m is not None:
        ef, el = f_end - true_f_m, l_end - true_l_m
        out.update(end_err_f_m=ef, end_err_l_m=el, end_err_m=math.hypot(ef, el),
                   end_err_pct_of_path=100.0 * math.hypot(ef, el) / L if L > 0 else None)
    if true_distance_m is not None:
        out["distance_err_m"] = out["displacement_m"] - true_distance_m
    if true_turn_deg is not None:
        out["turn_err_deg"] = turn - true_turn_deg
    return out


def truth_from_info(info: dict) -> Dict:
    """錄音 meta 每段的 info → metrics() 的實際值參數。
    tools/track_odometry.py 存 true_f_m / true_l_m / true_turn_deg；光流校正錄音存 distance_mm、angle_deg。"""
    t: Dict = {}
    if info.get("true_f_m") is not None and info.get("true_l_m") is not None:
        t["true_f_m"], t["true_l_m"] = float(info["true_f_m"]), float(info["true_l_m"])
    if info.get("distance_mm") is not None:
        t["true_distance_m"] = float(info["distance_mm"]) / 1000.0
    for k in ("true_turn_deg", "angle_deg"):
        if info.get(k) is not None:
            t["true_turn_deg"] = float(info[k])
    return t


# ----------------------------------------------------------------------
# 畫圖（OpenCV；圖上只用 ASCII，Hershey 字型沒有中文）
# ----------------------------------------------------------------------
def _nice_step(span_m: float) -> float:
    for s in (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0):
        if span_m / s <= 12:
            return s
    return 20.0


def render(sf: Sequence[Tuple[float, float]], size: Tuple[int, int] = (800, 800), grid_m: Optional[float] = None,
           lines: Sequence[str] = (), truth_end: Optional[Tuple[float, float]] = None,
           heading_deg: Optional[float] = None, min_span_m: float = 1.0):
    """畫起點座標的軌跡：上 = 起點車頭方向（+f）、左 = +l。回傳 BGR 影像。

    sf         每一點的 (f, l)（公尺），Track.start_frame()
    grid_m     格線間距（公尺）；None = 依範圍自動（0.1/0.2/0.5/1/2/5 m）
    truth_end  捲尺量的終點 (f*, l*)，畫成十字
    heading_deg 目前相對起點的轉角（畫在終點的小箭頭；左轉為正）
    """
    import cv2
    import numpy as np

    W, H = size
    top = 22 * (len(lines) + 1)
    img = np.full((H, W, 3), 255, np.uint8)
    pts = list(sf) or [(0.0, 0.0)]
    extra = [truth_end] if truth_end is not None else []
    fs = [p[0] for p in pts + extra] + [0.0]
    ls = [p[1] for p in pts + extra] + [0.0]
    # 螢幕座標：u 往右 = −l，v 往下 = −f
    u_lo, u_hi = -max(ls), -min(ls)
    v_lo, v_hi = -max(fs), -min(fs)
    span = max(u_hi - u_lo, v_hi - v_lo, min_span_m)
    cu, cv_ = (u_lo + u_hi) / 2, (v_lo + v_hi) / 2
    pad = 40
    scale = min(W - 2 * pad, H - top - 2 * pad) / (span * 1.1)
    ox, oy = W / 2 - cu * scale, top + (H - top) / 2 - cv_ * scale

    def px(f, l):
        return int(round(ox + (-l) * scale)), int(round(oy + (-f) * scale))

    step = grid_m if grid_m else _nice_step(span)
    # 格線：f、l 都是 step 的整數倍。畫面範圍 l ∈ [−(W − ox)/scale, ox/scale]、f ∈ [−(H − oy)/scale, (oy − top)/scale]
    for k in range(math.floor(-(W - ox) / scale / step), math.ceil(ox / scale / step) + 1):
        x, _ = px(0, k * step)
        if 0 <= x < W:
            cv2.line(img, (x, top), (x, H), (225, 225, 225), 1)
    for k in range(math.floor(-(H - oy) / scale / step), math.ceil((oy - top) / scale / step) + 1):
        _, y = px(k * step, 0)
        if top <= y < H:
            cv2.line(img, (0, y), (W, y), (225, 225, 225), 1)
    # 起點座標軸
    cv2.line(img, px(0, 0), px(0.25 * span, 0), (180, 180, 180), 1)
    cv2.putText(img, "start heading", (px(0.25 * span, 0)[0] + 4, px(0.25 * span, 0)[1]),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
    # 軌跡
    if len(pts) >= 2:
        poly = np.array([px(f, l) for f, l in pts], np.int32).reshape(-1, 1, 2)
        cv2.polylines(img, [poly], False, (200, 90, 20), 2, cv2.LINE_AA)
    cv2.circle(img, px(*pts[0]), 6, (60, 160, 60), -1)
    end = px(*pts[-1])
    cv2.circle(img, end, 6, (40, 40, 220), -1)
    if heading_deg is not None:
        a = math.radians(heading_deg)
        tip = px(pts[-1][0] + 0.08 * span * math.cos(a), pts[-1][1] + 0.08 * span * math.sin(a))
        cv2.arrowedLine(img, end, tip, (40, 40, 220), 2, tipLength=0.35)
    if truth_end is not None:
        cv2.drawMarker(img, px(*truth_end), (0, 0, 0), cv2.MARKER_TILTED_CROSS, 16, 2)
    # 比例尺與說明
    cv2.line(img, (20, H - 15), (20 + int(step * scale), H - 15), (0, 0, 0), 2)
    cv2.putText(img, f"{step:g} m (grid)", (24 + int(step * scale), H - 11), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1)
    legend = "green = start   red = end (arrow: heading)" + ("   X = measured end" if truth_end is not None else "")
    for i, t in enumerate(list(lines) + [legend]):
        cv2.putText(img, t, (10, 18 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    return img


def summary_lines(name: str, m: Dict) -> List[str]:
    """畫在圖上的 ASCII 說明。"""
    if m.get("samples", 0) < 1 or "path_m" not in m:
        return [f"{name}: no data"]
    out = [f"{name}  path L={m['path_m']:.2f} m  end (f,l)=({m['end_f_m']:+.2f},{m['end_l_m']:+.2f}) m  "
           f"D={m['displacement_m']:.2f} m  turn={m['turn_deg']:+.1f} deg"]
    err = []
    if m.get("end_err_m") is not None:
        pct = m["end_err_pct_of_path"]
        err.append(f"end err {m['end_err_m']:.3f} m" + (f" ({pct:.1f}% of L)" if pct is not None else ""))
    if m.get("distance_err_m") is not None:
        err.append(f"D err {m['distance_err_m']:+.3f} m")
    if m.get("turn_err_deg") is not None:
        err.append(f"turn err {m['turn_err_deg']:+.1f} deg")
    err.append(f"squal med {m['squal_median']:.0f} p5 {m['squal_p5']}  gaps {m['gaps']}")
    out.append("  ".join(err))
    return out
