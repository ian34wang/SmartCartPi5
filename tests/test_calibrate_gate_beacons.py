"""門口校正（core/gate_calibration.py 與 tools/calibrate_gate_beacons.py）的測試——不需要 BLE 硬體。

用合成的「推車來回走過門口」連續錄音（差分隨位置是階梯、加上雜訊與單邊衰減，
門內那顆刻意比門外強 3 dB）驗證：
    - 挑出來的參數交給執行期的 GateMonitor 重播，每一趟剛好判出一次、方向正確
    - 偏移量 ±2 dB 漂移時也通過；偏移量抓到門內那顆多出來的 3 dB
    - 檢查會抓到漏判、多判、停在門檻線來回跳
    - 分不開時判為不夠；方向反了、只走一個方向時報錯
    - 存檔（含 meta 的分段時間）→ 讀回來重算，結果一樣（--from-data）
    - 靜止剖面（--survey）的統計與提示
    - 量測前清掉佇列裡累積的舊封包
    - 用假的掃描器把整個互動錄音流程跑一遍

跑法（在專案根目錄）：
    python3 tests/test_calibrate_gate_beacons.py
"""
import builtins
import math
import queue
import random
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.calibrate_gate_beacons as tool  # noqa: E402
from core.gate_calibration import (  # noqa: E402
    DRIFT_DB, Mark, Session, _with, calibrate_session, check_session, segment_stats, survey_notes,
)
from core.gate_model import GateParams, evaluate  # noqa: E402
from core.gate_monitor import GateMonitor  # noqa: E402
from core.gate_recorder import GateRecorder, load  # noqa: E402
from drivers.ble_beacon_scanner import BeaconObservation  # noqa: E402

IN, OUT = "GATE-INSIDE", "GATE-OUTSIDE"
FAILURES = []
BASE = GateParams(IN, OUT, smoothing_sec=1.0, estimator="p75", min_samples=3, min_confirm_samples=3,
                  hysteresis_dbm=4.0, min_crossing_rssi_dbm=-60.0, offsets={IN: 0.0, OUT: 0.0})
FAST = dict(estimators=("p75", "max"), windows=(1.0, 1.5), confirms=(3, 5))   # 測試用的小網格


def check(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {extra}")
        FAILURES.append(label)


# ----------------------------------------------------------------------
# 合成資料
# ----------------------------------------------------------------------
def levels(x, step=10.0, bias_in=3.0):
    """推車在位置 x（m，<0 門外、>0 門內）時兩顆的真實 RSSI。門內那顆天生強 bias_in dB。"""
    k = math.tanh(x / 0.3) * step / 2
    far = 2.0 * max(0.0, abs(x) - 2.0)
    return -55 + bias_in + k - far, -55 - k - far


class Synth:
    """依位置軌跡產生兩顆 Beacon 的封包。"""

    def __init__(self, seed=0, noise=1.5, fade=0.2, depth=10.0, rate=8.0, **lv):
        self.rnd = random.Random(seed)
        self.noise, self.fade, self.depth, self.rate, self.lv = noise, fade, depth, rate, lv
        self.obs, self.marks, self.t = [], [], 1000.0

    def _emit(self, t0, dur, xf):
        for bid in (IN, OUT):
            t = t0 + self.rnd.random() * 0.1
            while t < t0 + dur:
                li, lo = levels(xf(t - t0), **self.lv)
                v = (li if bid == IN else lo) + self.rnd.gauss(0, self.noise)
                if self.rnd.random() < self.fade:
                    v -= self.depth   # 多路徑：只會往下掉
                self.obs.append((t, bid, float(round(v)), None))
                t += self.rnd.expovariate(self.rate)

    def move(self, label, x0, x1, move_sec, still_sec):
        t0 = self.t
        self._emit(t0, move_sec + still_sec,
                   lambda dt: x0 + (x1 - x0) * min(dt / move_sec, 1.0) if move_sec > 0 else x1)
        self.t += move_sec + still_sec
        self.marks.append(Mark(label, t0, self.t))

    def session(self):
        return Session(sorted(self.obs), list(self.marks))


def walk_session(trips=4, seed=0, hold=True, far=True, speed=1.0, still=2.0, **kw):
    s = Synth(seed=seed, **kw)
    for i in range(1, trips + 1):
        s.move(f"enter{i}", -2.0, 2.0, 4.0 / speed, still)
        s.move(f"exit{i}", 2.0, -2.0, 4.0 / speed, still)
    if hold:
        s.move("to_hold", -2.0, 0.0, 2.0 / speed, 0.0)
        s.move("hold", 0.0, 0.0, 0.0, 20.0)
    if far:
        s.move("to_far", 0.0, 4.0, 4.0 / speed, 0.0)
        s.move("far", 4.0, 4.0, 0.0, 20.0)
    return s.session()


def monitor_events(obs, params):
    m = GateMonitor.from_params(params)
    out = []
    for t, bid, rssi, _ in obs:
        e = m.process_observation(bid, rssi, t)
        if e:
            out.append((t, e))
    return out


# ----------------------------------------------------------------------
print("\n[1] 一般情況：挑出的參數交給執行期 GateMonitor，每一趟剛好判對一次")
sess = walk_session()
t0 = time.time()
cal = calibrate_session(sess, BASE)
print(f"        （完整網格計算 {time.time() - t0:.1f} 秒）")
check(cal.error is None and cal.verdict in ("可以", "勉強"), "判讀為可以或勉強", (cal.verdict, cal.error))
p = cal.params
check(p is not None and cal.check.passed, "建議參數本身重播全部通過", cal.check)
ev = monitor_events(sess.obs, p)
in_passes = [e for t, e in ev if sess.passes[0].t0 <= t <= sess.passes[-1].t1 + p.smoothing_sec + 1.0]
check(in_passes == ["entering", "exiting"] * 4, "執行期 GateMonitor：走過門口的 8 趟 = 進出交替各一次", in_passes)
quiet_ev = {m.label: sum(1 for t, _ in ev if m.t0 <= t <= m.t1) for m in sess.quiet}
check(all(n <= 1 for n in quiet_ev.values()), "停在門檻線、店內深處都沒有來回跳", quiet_ev)
check(not any(e == "exiting" for t, e in ev if sess.quiet[-1].t0 <= t <= sess.quiet[-1].t1),
      "停在店內深處沒有判成出場")
evals = evaluate(sess.obs, p)
drift_ok = all(check_session(evals, _with(p, p.offset_inside + d, p.hysteresis_dbm, p.min_crossing_rssi_dbm),
                             sess).passed for d in (-DRIFT_DB, DRIFT_DB))
check(drift_ok, f"門內偏移量 ±{DRIFT_DB:g} dB 漂移時也通過")
check(abs(p.offset_inside - (-3.0)) <= 1.0, "偏移量抓到門內那顆多出來的 3 dB（約 −3）", p.offset_inside)
check(cal.chosen.interval[0] <= p.hysteresis_dbm <= cal.chosen.interval[1], "遲滯取在可行範圍內",
      (p.hysteresis_dbm, cal.chosen.interval))
check(all(h is not None and h > 0 for h in cal.check.pass_results), "每一趟都記錄了判出時間", cal.check.pass_results)

print("\n[2] 檢查會抓到漏判、多判、來回跳")
ck = check_session(evals, replace(p, hysteresis_dbm=30.0), sess)
check(not ck.passed and "沒有判出來" in ck.reason and ck.correct == 0, "遲滯大到判不出來 → 第 1 趟漏判", ck.reason)
noisy = walk_session(seed=3, noise=4.0, fade=0.35, depth=14.0)
ck2 = check_session(evaluate(noisy.obs, replace(BASE, estimator="p75")),
                    replace(BASE, hysteresis_dbm=0.5, min_confirm_samples=1, min_crossing_rssi_dbm=-90.0), noisy)
check(not ck2.passed and ("多判" in ck2.reason or "來回跳" in ck2.reason), "遲滯太小 → 抓到多判或來回跳", ck2.reason)
ck3 = check_session(evals, p, sess, early_exit=False)
check(ck3.passed and ck3.correct == 8, "不提早結束時逐趟結果完整", (ck3.correct, ck3.reason))

print("\n[3] 分不開 → 判為不夠，不給參數")
weak = walk_session(seed=5, step=2.0, noise=3.0, fade=0.3, depth=12.0)
cal3 = calibrate_session(weak, BASE, **FAST)
check(cal3.params is None and cal3.verdict == "不夠", "門內外只差 2 dB → 不夠", (cal3.verdict, cal3.error))
check(len(cal3.candidates) > 0 and all(c.interval is None for c in cal3.candidates), "每一組候選都列出卡在哪")

print("\n[4] 方向反了、只走一個方向時報錯")
rev = walk_session(seed=1, step=-10.0, bias_in=0.0)
cal4 = calibrate_session(rev, BASE, **FAST)
check(cal4.verdict == "錯誤" and cal4.error and "方向反了" in cal4.error, "門內比門外低 → 提示裝反", cal4.error)
one = Session(sess.obs, [m for m in sess.marks if m.label.startswith("enter")])
cal5 = calibrate_session(one, BASE, **FAST)
check(cal5.verdict == "錯誤" and cal5.error is not None, "只有進場 → 報錯", cal5.error)

print("\n[5] 存檔 → 讀回來重算，結果一樣（--from-data）")
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "observations.csv"
    rec = GateRecorder(path, kind="calibration_walk")
    rec.start(BASE)
    for t, bid, rssi, yaw in sess.obs:
        label = next((m.label for m in sess.marks if m.t0 <= t < m.t1), "far")
        rec.record_observation(t, bid, rssi, yaw, None, segment=label)
    rec.write_meta(BASE, marks=[[m.label, m.t0, m.t1] for m in sess.marks])
    rec.close()
    segs, _, meta = load(path)
    s2 = Session.from_recording(segs, meta["marks"])
    check(len(s2.obs) == len(sess.obs) and [m.label for m in s2.passes] == [m.label for m in sess.passes],
          "讀回的觀測數、分段與原本相同")
    cal6 = calibrate_session(s2, BASE)
    check(cal6.params == cal.params, "從檔案重算的參數與直接算的完全相同", (cal6.params, cal.params))
    s3 = Session.from_recording(segs)   # meta 沒有 marks 時用每段頭尾
    check([m.kind for m in s3.passes] == [m.kind for m in sess.passes], "沒有 marks 時也能從段落名稱還原趟次")

print("\n[6] 靜止剖面（--survey）")
surv = {}
for i, (key, x) in enumerate((("out100", -1.0), ("out50", -0.5), ("thr", 0.0), ("in50", 0.5), ("in100", 1.0))):
    s = Synth(seed=100 + i)   # 固定種子（hash() 每次執行都不一樣）
    s.move(key, x, x, 0.0, 15.0)
    surv[key] = s.session().obs
stats = {k: segment_stats(v, BASE) for k, v in surv.items()}
check(all(st is not None for st in stats.values()), "每個位置都算得出統計")
check(stats["in100"].delta > stats["thr"].delta > stats["out100"].delta, "差分隨位置由外到內遞增",
      {k: round(st.delta, 1) for k, st in stats.items()})
check(6.5 < stats["out100"].rate_in < 9.5, "封包率算對（約 8 包/秒）", stats["out100"].rate_in)
notes = survey_notes(stats, replace(BASE, hysteresis_dbm=14.0))
check(any("門外" in n for n in notes) and any("門內" in n for n in notes), "平台不夠寬時兩側都提示", notes)

print("\n[7] 量測前清掉佇列裡累積的舊封包")


class FakeScanner:
    def __init__(self):
        self.out_queue = queue.Queue()


fs = FakeScanner()
old_t = time.time() - 30
for i in range(500):
    fs.out_queue.put(BeaconObservation(IN, -40, old_t + i * 0.01))
got = tool.collect(fs, [IN, OUT], 0.3)
check(len(got) == 0, "走位期間累積的 500 包一筆都沒算進來", len(got))

print("\n[8] 用假的掃描器把互動錄音流程跑一遍（record_walkthrough）")


class LiveFake(FakeScanner):
    """依「目前推車位置」即時產生封包；位置由假的 input() 控制。"""

    def __init__(self):
        super().__init__()
        self.rnd = random.Random(7)
        self.x0 = self.x1 = -2.0
        self.t0 = time.time()
        self.move = 1.0
        self.stop_ev = threading.Event()
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def go(self, x1, move):
        self.x0, self.x1, self.t0, self.move = self.x(), x1, time.time(), move

    def x(self):
        f = min((time.time() - self.t0) / self.move, 1.0)
        return self.x0 + (self.x1 - self.x0) * f

    def _run(self):
        while not self.stop_ev.is_set():
            li, lo = levels(self.x())
            for bid, lv in ((IN, li), (OUT, lo)):
                v = lv + self.rnd.gauss(0, 1.0) - (8 if self.rnd.random() < 0.15 else 0)
                self.out_queue.put(BeaconObservation(bid, int(round(v)), time.time()))
            time.sleep(1 / 15)


live = LiveFake()
MOVE, STILL = 1.2, 1.6


def fake_input(prompt=""):
    if "進場" in prompt:
        live.go(2.0, MOVE)
    elif "出場" in prompt:
        live.go(-2.0, MOVE)
    elif "門檻線" in prompt:
        live.go(0.0, 0.6)
        time.sleep(0.6)
        return ""
    elif "店內深處" in prompt:
        return "s"
    else:
        return ""
    time.sleep(MOVE + STILL)
    return ""


real_input, real_hold = builtins.input, tool.HOLD_SEC
builtins.input, tool.HOLD_SEC = fake_input, 4.0
try:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "observations.csv"
        rec = GateRecorder(path, kind="calibration_walk")
        rec.start(BASE)
        s8 = tool.record_walkthrough(live, BASE, 2, rec)
        rec.close()
        live.stop_ev.set()
        check(s8 is not None and [m.label for m in s8.marks] == ["enter1", "exit1", "enter2", "exit2",
                                                                  "to_hold", "hold", "to_far"],
              "分段標記正確（店內深處選擇不做）", None if s8 is None else [m.label for m in s8.marks])
        segs8, _, meta8 = load(path)
        check(sum(len(v) for v in segs8.values()) == len(s8.obs), "記錄檔筆數 = 錄到的筆數")
        check(len(meta8.get("marks", [])) == len(s8.marks), "meta 存了分段時間")
        cal8 = calibrate_session(Session.from_recording(segs8, meta8["marks"]), BASE, **FAST)
        check(cal8.params is not None and cal8.check.passed, "這段錄音校正得出來、重播 4 趟全對",
              (cal8.verdict, cal8.error, cal8.check and cal8.check.reason))
finally:
    builtins.input, tool.HOLD_SEC = real_input, real_hold
    live.stop_ev.set()

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("全部通過")
