"""
core/gate_calibration.py

門口校正的計算（純邏輯，沒有 I/O）。量測與互動在 tools/calibrate_gate_beacons.py。

標準只有一條：**拿真的推車走過門口的錄音，重播真正的判定規則（core/gate_model.py），
每一趟都要判對。** 參數不是用公式推出來的，是把候選參數丟進跟執行期一模一樣的判定
流程，看它在實際走過門口的資料上會怎樣。

為什麼要「實際走過」（2026-10-28 的教訓）：
    前一版是站在 5 個固定位置各量 15 秒，再把這幾段接起來「模擬走過」。但這等於
    推車在每個位置停 15 秒——濾波器有充分時間穩定，判定很容易確認。用同一份資料
    改成每個位置只停 1 秒（推車 0.5 m/s）重算，前一版建議的參數「門外走到門內」
    15 次只判對 5 次。靜止量測模擬不出推車移動時的延遲、車體與人的遮擋、Pi 的朝向
    變化，所以改成直接錄真的走過。

錄音流程（tools/calibrate_gate_beacons.py 會一步一步提示）：
    推車從門外約 2 m 出發 → 推到門內約 2 m 停下 → 推回門外約 2 m 停下 → 重複 N 次
    → 推到門檻線上停 20 秒 → （選做）推到店內深處停 20 秒
    全程連續錄，每一段的起訖時間都記下來。

挑參數：
    偏移量   每一趟結束時推車是停著的：進場那幾趟的結尾是「門內靜止」、出場那幾趟
             的結尾是「門外靜止」。偏移量取兩者差分中位數的中點。
    絕對門檻 這些靜止結尾裡，佔優勢那顆校正後強度的最小值 − 5 dB。
    其餘     估計量（p75 / p90 / max）× 窗口（1.0 / 1.5 / 2.0 秒）× 連續確認筆數
             （3 / 5 / 8）× 遲滯（2~12 dB 每 0.25）全部試，通過下列檢查的才算可行；
             選「可行遲滯範圍最寬」的組合，取範圍中間的遲滯。一樣寬時優先保留現在
             的設定、再來是比較短的窗口與比較少的確認筆數（判定比較快）。

檢查（整段錄音照時間順序一次重播，狀態會一路延續，跟實際執行一模一樣）：
    1. 每一趟都剛好判出一次對的事件（進場那趟 entering、出場那趟 exiting），
       而且發生在這一趟開始之後、結束後 (窗口 + 1) 秒以內（容許濾波延遲）
    2. 走過門口的這段期間沒有任何多餘的事件
    3. 停在門檻線那 20 秒：最多一次事件（剛停下時判定可能還在跟上），不能來回跳；
       停在店內深處那 20 秒：不能判成出場，也不能來回跳
    4. 以上在門內那顆偏移量 ±2 dB 時也都要成立（模擬校正之後的環境漂移）

另外保留「5 個位置的靜止剖面」（survey）：不挑參數，只用來看佈署的形狀——門外、
門內的平台夠不夠寬、門檻線附近雜訊多大，調擺法的時候用。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

from core.gate_model import Evaluation, GateDecision, GateParams, Observation, evaluate

ESTIMATORS = ("p75", "p90", "max")
WINDOWS = (1.0, 1.5, 2.0)
CONFIRMS = (3, 5, 8)
H_MIN, H_MAX, H_STEP = 2.0, 12.0, 0.25
DRIFT_DB = 2.0
ABS_MARGIN_DB = 5.0
GOOD_WIDTH_DB = 1.0
END_STILL_SEC = 1.5     # 每一趟結尾取這麼長的時間當「靜止」
HOLD_SEC = 20.0


# ======================================================================
# 錄音
# ======================================================================
@dataclass
class Mark:
    label: str      # enter1 / exit1 / … / to_hold / hold / to_far / far
    t0: float
    t1: float

    @property
    def kind(self) -> Optional[str]:
        if self.label.startswith("enter"):
            return "entering"
        if self.label.startswith("exit"):
            return "exiting"
        return None


@dataclass
class Session:
    obs: List[Observation]
    marks: List[Mark]

    @property
    def passes(self) -> List[Mark]:
        return [m for m in self.marks if m.kind is not None]

    @property
    def quiet(self) -> List[Mark]:
        return [m for m in self.marks if m.label in ("hold", "far")]

    @classmethod
    def from_recording(cls, segments: Dict[str, List[Observation]], marks: Optional[list] = None) -> "Session":
        """讀回記錄檔。marks 是 meta 裡存的 [[label, t0, t1], ...]；沒有的話用每段的頭尾時間。"""
        obs = sorted((o for seg in segments.values() for o in seg), key=lambda o: o[0])
        if marks:
            ms = [Mark(l, float(a), float(b)) for l, a, b in marks]
        else:
            ms = [Mark(l, min(o[0] for o in v), max(o[0] for o in v)) for l, v in segments.items() if v]
            ms.sort(key=lambda m: m.t0)
        return cls(obs=obs, marks=ms)


# ======================================================================
# 檢查
# ======================================================================
@dataclass
class SessionCheck:
    passed: bool
    correct: int                      # 判對的趟數（照順序，遇到第一個錯就停）
    reason: str = ""
    events: List[Tuple[float, str]] = field(default_factory=list)
    pass_results: List[Optional[float]] = field(default_factory=list)  # 每趟事件發生在開始後幾秒


def check_session(evals: Sequence[Evaluation], params: GateParams, session: Session,
                  early_exit: bool = True) -> SessionCheck:
    """照時間順序重播一次（GateDecision，跟執行期同一份程式），邊走邊檢查。"""
    passes = session.passes
    quiet = session.quiet
    grace = params.smoothing_sec + 1.0
    zone_end = passes[-1].t1 + grace if passes else float("-inf")
    dec = GateDecision(params)
    idx = 0
    events: List[Tuple[float, str]] = []
    hits: List[Optional[float]] = [None] * len(passes)
    quiet_count: Dict[str, int] = {}
    reason = ""

    def fail(msg: str) -> SessionCheck:
        return SessionCheck(False, idx, msg, events, hits)

    for e in evals:
        # 這一趟已經過了期限還沒判出來 → 漏判
        while idx < len(passes) and e.t > passes[idx].t1 + grace:
            reason = reason or f"第 {idx + 1} 趟{_name(passes[idx])}沒有判出來"
            if early_exit:
                return fail(reason)
            idx += 1
        if e.s_in is None or e.s_out is None:
            continue
        st = dec.step(e.t, e.s_in, e.s_out, e.yaw)
        if not st.event:
            continue
        events.append((st.t, st.event))
        q = next((m for m in quiet if m.t0 <= st.t <= m.t1), None)
        if q is not None:
            # 剛停下時判定可能還在跟上，容許一次；但不能來回跳，店內深處也不能判成出場
            quiet_count[q.label] = quiet_count.get(q.label, 0) + 1
            bad = quiet_count[q.label] > 1 or (q.label == "far" and st.event == "exiting")
            if bad:
                where = "門檻線" if q.label == "hold" else "店內深處"
                reason = reason or (f"停在{where}時判定來回跳" if quiet_count[q.label] > 1
                                    else f"停在{where}時判成 {st.event}")
                if early_exit:
                    return fail(reason)
            continue
        if passes and passes[0].t0 <= st.t <= zone_end:
            if idx < len(passes) and st.event == passes[idx].kind and \
                    passes[idx].t0 <= st.t <= passes[idx].t1 + grace:
                hits[idx] = st.t - passes[idx].t0
                idx += 1
            else:
                where = f"第 {idx + 1} 趟" if idx < len(passes) else "最後一趟之後"
                reason = reason or f"{where}多判了一次 {st.event}"
                if early_exit:
                    return fail(reason)
    while idx < len(passes):
        if hits[idx] is None:
            reason = reason or f"第 {idx + 1} 趟{_name(passes[idx])}沒有判出來"
            if early_exit:
                return fail(reason)
        idx += 1
    ok = not reason
    return SessionCheck(ok, sum(1 for h in hits if h is not None) if not ok else len(passes), reason, events, hits)


def _name(m: Mark) -> str:
    return "（進場）" if m.kind == "entering" else "（出場）"


# ======================================================================
# 偏移量、絕對門檻
# ======================================================================
def end_levels(evals: Sequence[Evaluation], session: Session) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """每一趟結尾靜止那一段的平滑值中位數（原始、未加偏移）。回傳 (門內結尾們, 門外結尾們)。"""
    ins, outs = [], []
    for m in session.passes:
        pairs = [(e.s_in, e.s_out) for e in evals
                 if m.t1 - END_STILL_SEC <= e.t <= m.t1 and e.s_in is not None and e.s_out is not None]
        if len(pairs) < 3:
            continue
        lv = (statistics.median(a for a, _ in pairs), statistics.median(b for _, b in pairs))
        (ins if m.kind == "entering" else outs).append(lv)
    return ins, outs


def offset_and_threshold(evals: Sequence[Evaluation], session: Session) -> Tuple[Optional[float], Optional[float], dict]:
    ins, outs = end_levels(evals, session)
    if not ins or not outs:
        return None, None, {"kind": "missing", "error": "進場、出場至少各要有一趟結尾有足夠的資料。"}
    d_in = statistics.median(a - b for a, b in ins)
    d_out = statistics.median(a - b for a, b in outs)
    if d_in - d_out <= 0:
        return None, None, {"kind": "reversed", "error": (
            f"停在門內時的差分（{d_in:+.1f}）沒有比停在門外（{d_out:+.1f}）高——方向反了。"
            "檢查兩個開口是不是裝反，或 config.json 的 inside_id / outside_id 是不是對調了。")}
    offset = -(d_in + d_out) / 2.0
    levels = [a + offset for a, _ in ins] + [b for _, b in outs]
    return offset, min(levels) - ABS_MARGIN_DB, {"end_in": d_in, "end_out": d_out, "step": d_in - d_out}


# ======================================================================
# 搜尋
# ======================================================================
def _with(params: GateParams, offset: float, h: float, mc: Optional[float]) -> GateParams:
    return replace(params.with_offset_inside(offset), hysteresis_dbm=h, min_crossing_rssi_dbm=mc)


def _runs(values: List[float]) -> Optional[Tuple[float, float]]:
    best = None
    start = prev = None
    for h in values:
        if start is not None and abs(h - prev - H_STEP) < 1e-9:
            prev = h
            continue
        if start is not None and (best is None or prev - start > best[1] - best[0]):
            best = (start, prev)
        start = prev = h
    if start is not None and (best is None or prev - start > best[1] - best[0]):
        best = (start, prev)
    return best


@dataclass
class Candidate:
    estimator: str
    window: float
    confirm: int
    offset: float
    min_crossing: float
    info: dict
    passing: List[float]
    interval: Optional[Tuple[float, float]]
    best_partial: Tuple[int, float, str]   # 不含漂移時最多判對幾趟、在哪個遲滯、卡在哪

    @property
    def width(self) -> float:
        return -1.0 if self.interval is None else self.interval[1] - self.interval[0]


@dataclass
class Calibration:
    error: Optional[str]
    verdict: str                        # 可以 / 勉強 / 不夠 / 錯誤
    params: Optional[GateParams]
    chosen: Optional[Candidate]
    check: Optional[SessionCheck]       # 建議參數（不含漂移）的逐趟結果
    candidates: List[Candidate]
    n_passes: int


def calibrate_session(session: Session, base: GateParams,
                      estimators: Sequence[str] = ESTIMATORS,
                      windows: Sequence[float] = WINDOWS,
                      confirms: Sequence[int] = CONFIRMS,
                      progress=None) -> Calibration:
    n = len(session.passes)
    if n < 2 or not any(m.kind == "entering" for m in session.passes) \
            or not any(m.kind == "exiting" for m in session.passes):
        return Calibration("進場、出場至少各要走一趟。", "錯誤", None, None, None, [], n)
    base = replace(base, exit_yaw_deg=None)  # 校正時不做航向驗證
    ests = list(dict.fromkeys([base.estimator, *estimators]))
    wins = sorted({round(base.smoothing_sec, 3), *windows})
    confs = sorted({base.min_confirm_samples, *confirms})
    hs = [round(H_MIN + i * H_STEP, 2) for i in range(int(round((H_MAX - H_MIN) / H_STEP)) + 1)]

    cands: List[Candidate] = []
    error = None
    total = len(ests) * len(wins)
    done = 0
    for est in ests:
        for w in wins:
            done += 1
            if progress:
                progress(done, total)
            p = replace(base, estimator=est, smoothing_sec=w)
            evals = evaluate(session.obs, p)
            offset, mc, info = offset_and_threshold(evals, session)
            if offset is None:
                if info["kind"] == "reversed":
                    return Calibration(info["error"], "錯誤", None, None, None, [], n)
                error = info["error"]
                continue
            for conf in confs:
                pc = replace(p, min_confirm_samples=conf)
                passing: List[float] = []
                best = (-1, 0.0, "")
                for h in hs:
                    ck = check_session(evals, _with(pc, offset, h, mc), session)
                    if ck.correct > best[0]:
                        best = (ck.correct, h, ck.reason)
                    if not ck.passed:
                        continue
                    if all(check_session(evals, _with(pc, offset + d, h, mc), session).passed
                           for d in (-DRIFT_DB, DRIFT_DB)):
                        passing.append(h)
                cands.append(Candidate(est, w, conf, offset, mc, info, passing, _runs(passing), best))

    if not cands:
        return Calibration(error or "資料不足。", "錯誤", None, None, None, [], n)

    def pref(c: Candidate):
        same = (c.estimator == base.estimator) + (abs(c.window - base.smoothing_sec) < 1e-9) + \
               (c.confirm == base.min_confirm_samples)
        return (round(c.width, 2), same, -c.window, -c.confirm)

    feasible = [c for c in cands if c.interval is not None]
    if not feasible:
        return Calibration(None, "不夠", None, None, None, cands, n)
    chosen = max(feasible, key=pref)
    h = round((chosen.interval[0] + chosen.interval[1]) / 2.0 / H_STEP) * H_STEP
    params = _with(replace(base, estimator=chosen.estimator, smoothing_sec=chosen.window,
                           min_confirm_samples=chosen.confirm),
                   round(chosen.offset, 1), round(h, 2), round(chosen.min_crossing, 1))
    evals = evaluate(session.obs, params)
    check = check_session(evals, params, session, early_exit=False)
    verdict = "可以" if chosen.width >= GOOD_WIDTH_DB else "勉強"
    return Calibration(None, verdict, params, chosen, check, cands, n)


# ======================================================================
# 靜止剖面（survey，只看佈署形狀，不挑參數）
# ======================================================================
POSITIONS: List[Tuple[str, str, str, str]] = [
    ("out100", "門外 100 cm", "站在門外、距離門檻線約 1 公尺", "out"),
    ("out50", "門外 50 cm", "往門口走一半", "out"),
    ("thr", "門檻線", "正好在門檻線上（兩個鍋底貼合的那個平面）", "thr"),
    ("in50", "門內 50 cm", "跨進門內半公尺", "in"),
    ("in100", "門內 100 cm", "門內 1 公尺", "in"),
]
LABELS = {k: label for k, label, _, _ in POSITIONS}
SIDE = {k: s for k, _, _, s in POSITIONS}


@dataclass
class SegmentStats:
    delta: float        # 差分中位數（已加 params 的偏移）
    noise: float        # 差分標準差
    inside: float       # 門內平滑值中位數（已加偏移）
    outside: float      # 門外平滑值中位數
    judged_ratio: float
    rate_in: float
    rate_out: float


def segment_stats(obs: Sequence[Observation], params: GateParams) -> Optional[SegmentStats]:
    """一個靜止位置的統計（前一個窗口長度是暖機，不算）。"""
    evals = evaluate(obs, params)
    if not evals:
        return None
    t0 = evals[0].t
    warm = [e for e in evals if e.t >= t0 + params.smoothing_sec]
    pairs = [(e.s_in + params.offset_inside, e.s_out + params.offset_outside)
             for e in warm if e.s_in is not None and e.s_out is not None]
    if len(pairs) < 10:
        return None
    d = [a - b for a, b in pairs]
    times = [o[0] for o in obs]
    dur = max(times) - min(times) if len(times) > 1 else 0.0
    n_in = sum(1 for o in obs if o[1] == params.inside_id)
    return SegmentStats(
        delta=statistics.median(d), noise=statistics.pstdev(d),
        inside=statistics.median(a for a, _ in pairs), outside=statistics.median(b for _, b in pairs),
        judged_ratio=len(pairs) / len(warm) if warm else 0.0,
        rate_in=n_in / dur if dur > 0 else 0.0, rate_out=(len(obs) - n_in) / dur if dur > 0 else 0.0,
    )


def survey_notes(stats: Dict[str, Optional[SegmentStats]], params: GateParams) -> List[str]:
    """看剖面形狀，指出會讓判定變難的地方。用現在設定的遲滯當尺。"""
    h = params.hysteresis_dbm
    notes = []
    for key in ("out100", "out50"):
        s = stats.get(key)
        if s is not None and s.delta > -h:
            notes.append(f"{LABELS[key]}差分只有 {s.delta:+.1f} dB（沒有低於 −{h:g}）：門外這一側還沒確實偏向門外。")
    for key in ("in50", "in100"):
        s = stats.get(key)
        if s is not None and s.delta < h:
            notes.append(f"{LABELS[key]}差分只有 {s.delta:+.1f} dB（沒有高於 +{h:g}）：門內這一側還沒確實偏向門內。")
    thr = stats.get("thr")
    if thr is not None and thr.noise > h / 2:
        notes.append(f"門檻線上的雜訊 σ {thr.noise:.1f} dB，比遲滯的一半還大：推車停在門口時判定可能會來回跳。")
    return notes
