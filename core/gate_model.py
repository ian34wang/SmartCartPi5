"""
core/gate_model.py

管制區門口判定的「單一標準」：參數、濾波、判定、重播，全部在這一支。

    執行期    core/gate_monitor.GateMonitor          一筆一筆即時處理
    校正      core/gate_calibration                  用實測資料挑參數
    事後分析  tools/analyze_gate_log                  拿記錄重算、試別的參數

三個地方用的都是這裡的 SmoothingWindow + GateCrossingDetector，沒有任何一個
地方自己另外寫一套濾波或判定。所以「校正時重播通過」跟「實際跑起來會怎樣」是
同一件事，「記錄檔重播出來的事件」也會跟當時即時判定出來的一模一樣
（tests/test_gate_model.py 驗證這一點）。

判定流程（每收到一筆觀測就做一次）：
    1. 這顆 Beacon 的原始 RSSI 放進它自己的歷史
    2. 兩顆的歷史都只留時間窗內的：時間戳 t 滿足  t_now − W < t ≤ t_now
       （W = rssi_smoothing_sec，t_now = 這筆觀測的時間）
    3. 任一顆窗內樣本少於 rssi_min_samples → 這一刻不判定
    4. 兩顆各自用 rssi_estimator（預設 p75）壓成一個值，再各加上自己的
       rssi_offset_db → s_in、s_out
    5. 差分 d = s_in − s_out 餵給 GateCrossingDetector：
         d ≥ +H 視為偏門內，d ≤ −H 視為偏門外，中間不改判定（H = 遲滯）
         偏的那一側校正後強度要 ≥ gate_min_crossing_rssi_dbm 才算數
         連續 gate_min_confirm_samples 筆都偏同一側才確認；確認的那一側
         跟上一次確認的不同，就是一次 entering / exiting
    6. 有設定 gate_exit_yaw_deg 時，再用當下 IMU 航向交叉驗證，不符就不採信
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from core.landmark_correction import (
    GATE_CROSSING_ENTERING,
    GateCrossingDetector,
    is_heading_consistent,
    smooth_rssi,
)

# 一筆觀測：(時間戳, beacon_id, 原始 RSSI, 當下航向角或 None)
Observation = Tuple[float, str, float, Optional[float]]


@dataclass(frozen=True)
class GateParams:
    """門口判定用到的全部參數。config.json、校正結果、記錄檔的 meta 都用這個格式。"""

    inside_id: str
    outside_id: str
    smoothing_sec: float = 1.0
    estimator: str = "p75"
    min_samples: int = 3
    hysteresis_dbm: float = 4.0
    min_crossing_rssi_dbm: Optional[float] = None
    min_confirm_samples: int = 3
    offsets: Dict[str, float] = field(default_factory=dict)
    exit_yaw_deg: Optional[float] = None
    heading_tolerance_deg: float = 45.0

    @classmethod
    def from_landmark_config(cls, cfg: dict) -> "GateParams":
        """吃 core.landmark_correction.load_landmark_config() 的結果。"""
        pair = cfg.get("gate_beacon_pair", {})
        return cls(
            inside_id=pair.get("inside_id", ""),
            outside_id=pair.get("outside_id", ""),
            smoothing_sec=float(cfg.get("rssi_smoothing_sec", 1.0)),
            estimator=cfg.get("rssi_estimator", "p75"),
            min_samples=int(cfg.get("rssi_min_samples", 3)),
            hysteresis_dbm=float(cfg.get("gate_hysteresis_dbm", 4.0)),
            min_crossing_rssi_dbm=cfg.get("gate_min_crossing_rssi_dbm"),
            min_confirm_samples=int(cfg.get("gate_min_confirm_samples", 3)),
            offsets=dict(cfg.get("rssi_offsets") or {}),
            exit_yaw_deg=cfg.get("gate_exit_yaw_deg"),
            heading_tolerance_deg=float(cfg.get("gate_heading_tolerance_deg", 45.0)),
        )

    @classmethod
    def from_dict(cls, d: dict) -> "GateParams":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def to_dict(self) -> dict:
        return asdict(self)

    def with_offset_inside(self, offset_inside: float) -> "GateParams":
        """門外那顆固定當基準（偏移 0），只調門內那顆。"""
        return replace(self, offsets={self.inside_id: offset_inside, self.outside_id: 0.0})

    @property
    def offset_inside(self) -> float:
        return float(self.offsets.get(self.inside_id, 0.0))

    @property
    def offset_outside(self) -> float:
        return float(self.offsets.get(self.outside_id, 0.0))


class SmoothingWindow:
    """兩顆 Beacon 的時間窗與平滑（判定流程的第 1~4 步，不含偏移量）。

    執行期與重播都用這一個類別，所以兩者的窗口邊界、樣本數門檻、估計量完全一致。
    """

    MAX_HISTORY = 200  # 時間戳異常時的安全上限

    def __init__(self, inside_id: str, outside_id: str, smoothing_sec: float,
                 estimator: str, min_samples: int):
        self.inside_id = inside_id
        self.outside_id = outside_id
        self.smoothing_sec = smoothing_sec
        self.estimator = estimator
        self.min_samples = max(1, min_samples)
        self.history: Dict[str, List[Tuple[float, float]]] = {inside_id: [], outside_id: []}

    def add(self, beacon_id: str, rssi: float, t: float) -> Optional[Tuple[float, float]]:
        """加入一筆，回傳 (門內平滑值, 門外平滑值)（原始 dBm、未加偏移）；樣本不夠回 None。"""
        if beacon_id not in self.history:
            return None
        self.history[beacon_id].append((t, float(rssi)))
        cutoff = t - self.smoothing_sec
        for hist in self.history.values():
            i = 0
            while i < len(hist) and hist[i][0] <= cutoff:
                i += 1
            if i:
                del hist[:i]
            if len(hist) > self.MAX_HISTORY:
                del hist[:-self.MAX_HISTORY]
        return self.current()

    def current(self) -> Optional[Tuple[float, float]]:
        ih = [v for _, v in self.history[self.inside_id]]
        oh = [v for _, v in self.history[self.outside_id]]
        if len(ih) < self.min_samples or len(oh) < self.min_samples:
            return None
        return (smooth_rssi(ih, len(ih), self.estimator), smooth_rssi(oh, len(oh), self.estimator))

    def counts(self) -> Tuple[int, int]:
        return len(self.history[self.inside_id]), len(self.history[self.outside_id])


@dataclass
class Evaluation:
    """判定流程第 4 步的結果（每筆觀測一個）。s_in/s_out 是原始 dBm、未加偏移。"""

    t: float
    s_in: Optional[float]
    s_out: Optional[float]
    yaw: Optional[float]


@dataclass
class DetectorStep:
    """判定流程第 5、6 步，每筆有效評估一個。"""

    t: float
    s_in: float          # 已加偏移
    s_out: float         # 已加偏移
    diff: float
    dominant: Optional[str]   # 這一筆之後已確認的一側："a" 門內 / "b" 門外 / None
    event: Optional[str]      # entering / exiting / None
    rejected: bool = False    # 被航向交叉驗證擋下


class GateDecision:
    """判定流程第 5、6 步：偏移量 + GateCrossingDetector + 航向交叉驗證。"""

    def __init__(self, params: GateParams):
        self.params = params
        self.detector = GateCrossingDetector(
            hysteresis_dbm=params.hysteresis_dbm,
            min_crossing_rssi_dbm=params.min_crossing_rssi_dbm,
            min_confirm_samples=params.min_confirm_samples,
        )
        self.rejected_by_heading = 0

    @property
    def dominant(self) -> Optional[str]:
        return self.detector._dominant

    def step(self, t: float, s_in_raw: float, s_out_raw: float, yaw: Optional[float]) -> DetectorStep:
        s_in = s_in_raw + self.params.offset_inside
        s_out = s_out_raw + self.params.offset_outside
        crossing = self.detector.add_sample(s_in, s_out, t)
        rejected = False
        if crossing is not None and not self._heading_agrees(crossing, yaw):
            self.rejected_by_heading += 1
            rejected = True
        return DetectorStep(t=t, s_in=s_in, s_out=s_out, diff=s_in - s_out,
                            dominant=self.dominant, event=None if rejected else crossing,
                            rejected=rejected)

    def _heading_agrees(self, crossing: str, yaw: Optional[float]) -> bool:
        p = self.params
        if p.exit_yaw_deg is None or yaw is None:
            return True
        expected = p.exit_yaw_deg + (180.0 if crossing == GATE_CROSSING_ENTERING else 0.0)
        return is_heading_consistent(yaw, expected, p.heading_tolerance_deg)


# ----------------------------------------------------------------------
# 重播
# ----------------------------------------------------------------------
def evaluate(observations: Iterable[Observation], params: GateParams) -> List[Evaluation]:
    """判定流程第 1~4 步（跟 GateMonitor 同一個 SmoothingWindow）。結果只跟窗口/估計量有關，
    跟偏移量、遲滯、門檻無關，所以掃參數時可以算一次重複用。"""
    win = SmoothingWindow(params.inside_id, params.outside_id, params.smoothing_sec,
                          params.estimator, params.min_samples)
    out: List[Evaluation] = []
    for t, bid, rssi, yaw in sorted(observations, key=lambda o: o[0]):
        if bid not in win.history:
            continue
        pair = win.add(bid, rssi, t)
        out.append(Evaluation(t, pair[0] if pair else None, pair[1] if pair else None, yaw))
    return out


@dataclass
class ReplayResult:
    events: List[Tuple[float, str]]
    dominant: Optional[str]
    steps: List[DetectorStep]
    evaluations: int
    judged: int


def decide(evaluations: Sequence[Evaluation], params: GateParams) -> ReplayResult:
    """判定流程第 5、6 步。"""
    dec = GateDecision(params)
    steps: List[DetectorStep] = []
    events: List[Tuple[float, str]] = []
    for e in evaluations:
        if e.s_in is None or e.s_out is None:
            continue
        st = dec.step(e.t, e.s_in, e.s_out, e.yaw)
        steps.append(st)
        if st.event:
            events.append((st.t, st.event))
    return ReplayResult(events=events, dominant=dec.dominant, steps=steps,
                        evaluations=len(evaluations), judged=len(steps))


def replay(observations: Iterable[Observation], params: GateParams) -> ReplayResult:
    """完整的判定流程，跟 GateMonitor 即時處理同一批觀測的結果一模一樣。"""
    return decide(evaluate(observations, params), params)


def concat(segments: Sequence[Sequence[Observation]], gap_sec: float = 0.1) -> List[Observation]:
    """把好幾段各自量的觀測接成一段：每段的時間平移到緊接在前一段之後。

    用來模擬推車依序經過幾個位置（例如門外 → 門檻線 → 門內）。
    """
    out: List[Observation] = []
    clock = 0.0
    for seg in segments:
        seg = sorted(seg, key=lambda o: o[0])
        if not seg:
            continue
        base = seg[0][0]
        for t, bid, rssi, yaw in seg:
            out.append((clock + (t - base), bid, rssi, yaw))
        clock = out[-1][0] + gap_sec
    return out
