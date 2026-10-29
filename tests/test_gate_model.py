"""core/gate_model.py（單一判定標準）的測試——不需要 BLE 硬體。

最重要的是第 1、2 項：執行期的 GateMonitor、事後重播、記錄檔重播，三者算出來的
結果必須一模一樣。這是「校正時驗證過的行為＝實際的行為」「記錄檔分析看到的＝當時
發生的」的根據。

跑法（在專案根目錄）：
    python3 tests/test_gate_model.py
"""
import math
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.gate_model import GateParams, concat, replay  # noqa: E402
from core.gate_monitor import GateMonitor  # noqa: E402
from core.gate_recorder import GateRecorder, load  # noqa: E402

IN, OUT = "GATE-INSIDE", "GATE-OUTSIDE"
FAILURES = []


def check(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {extra}")
        FAILURES.append(label)


def walk(seed=0, speed=0.5, length=8.0, rate=7.0, step_db=8.0, fade=0.2, yaw=None):
    """推車從門外 4 m 走到門內 4 m：差分隨位置是階梯（±step_db），加上雜訊與單邊衰減。"""
    rnd = random.Random(seed)
    obs = []
    dur = length / speed
    for bid, sign in ((IN, +1), (OUT, -1)):
        t = rnd.random() * 0.1
        while t < dur:
            x = -length / 2 + speed * t                 # <0 門外，>0 門內
            level = -60 + sign * step_db * math.tanh(x / 0.3) / 2
            v = level + rnd.gauss(0, 1.5) - (10 if rnd.random() < fade else 0)
            obs.append((1000.0 + t, bid, float(round(v)), yaw))
            t += rnd.expovariate(rate)
    return sorted(obs)


PARAMS = GateParams(IN, OUT, smoothing_sec=1.0, estimator="p75", min_samples=3,
                    hysteresis_dbm=3.0, min_crossing_rssi_dbm=-75.0, min_confirm_samples=3,
                    offsets={IN: 0.0, OUT: 0.0})


def run_online(obs, params, recorder=None, yaw_feed=None):
    m = GateMonitor.from_params(params, recorder=recorder)
    events = []
    for t, bid, rssi, yaw in obs:
        if yaw is not None:
            m.update_heading(yaw)
        e = m.process_observation(bid, rssi, t)
        if e:
            events.append((t, e))
    m.close()
    return events


print("\n[1] 執行期 GateMonitor 與重播結果一模一樣")
for seed in range(5):
    obs = walk(seed)
    online = run_online(obs, PARAMS)
    off = replay(obs, PARAMS).events
    check(online == off, f"seed {seed}：即時 {[e for _, e in online]} = 重播 {[e for _, e in off]}")
check([e for _, e in replay(walk(0), PARAMS).events] == ["entering"], "從門外走到門內剛好一次 entering")

print("\n[2] 記錄檔：寫下來、讀回來重播，跟當時即時判定一模一樣")
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "run.csv"
    obs = walk(3) + [(o[0] + 20.0, o[1], o[2], o[3]) for o in walk(4)]  # 走進來、回到門外、再走進來
    obs.sort()
    rec = GateRecorder(path, kind="test")
    online = run_online(obs, PARAMS, recorder=rec)
    segs, live, meta = load(path)
    p2 = GateParams.from_dict(meta["params"])
    check(p2 == PARAMS, "meta 裡的參數讀回來完全相同", (p2, PARAMS))
    r = replay(segs["run"], p2)
    check([(round(t, 6), e) for t, e in r.events] == [(round(t, 6), e) for t, e in online],
          f"記錄檔重播的事件 = 當時即時事件 {[e for _, e in online]}")
    live_diffs = [float(x["diff"]) for x in live["run"] if x["diff"]]
    check(len(live_diffs) == len(r.steps) and all(abs(a - s.diff) < 0.006 for a, s in zip(live_diffs, r.steps)),
          "每一筆記錄下來的差分值 = 重播算出的差分值", (len(live_diffs), len(r.steps)))
    live_events = [x["event"] for x in live["run"] if x["event"]]
    check(live_events == [e for _, e in online], "記錄檔的 event 欄 = 當時回傳的事件", live_events)

print("\n[3] 硬體沒接上就結束時，不會留下空的記錄檔")
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "x" / "never.csv"
    m = GateMonitor.from_params(PARAMS, recorder=GateRecorder(path, kind="test"))
    m.close()
    check(not path.exists(), "沒有任何觀測 → 不建立檔案")

print("\n[4] 航向交叉驗證：記錄檔重播也會擋下同樣的事件")
hp = GateParams(IN, OUT, smoothing_sec=1.0, min_samples=3, hysteresis_dbm=3.0, min_confirm_samples=3,
                offsets={IN: 0.0, OUT: 0.0}, exit_yaw_deg=0.0, heading_tolerance_deg=45.0)
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "yaw.csv"
    obs = walk(1, yaw=0.0)  # 走進來（entering 應朝 180°），但航向是 0° → 不採信
    online = run_online(obs, hp, recorder=GateRecorder(path, kind="test"))
    segs, live, meta = load(path)
    r = replay(segs["run"], GateParams.from_dict(meta["params"]))
    check(online == [] and r.events == [], "航向不符 → 即時與重播都沒有事件", (online, r.events))
    check(any(x["event"] == "rejected_entering" for x in live["run"]), "記錄檔有記下被擋下的那一次")

print("\n[5] concat：各段依序接起來，時間不重疊")
a = [(5.0, IN, -60.0, None), (6.0, OUT, -61.0, None)]
b = [(100.0, IN, -50.0, None), (100.5, OUT, -52.0, None)]
c = concat([a, b], gap_sec=0.1)
check([round(o[0], 6) for o in c] == [0.0, 1.0, 1.1, 1.6], "時間平移正確", c)

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("全部通過")
