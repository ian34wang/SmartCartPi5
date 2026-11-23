"""秤重校正（core/weight_calibration.py、tools/calibrate_weight.py）的測試——不需要硬體。

用合成的 HX711 讀數（已知 a、b，加雜訊、推車震動、放入時的衝擊振盪）驗證：
    - 多點最小平方找回正確的 offset、scale，殘差反映非線性
    - 未掃碼門檻是用狀態機重播推車震動找出來的：門檻以上不誤報、以下會誤報
    - 放入測試：正常的放入判成「正常加入」；衝擊太大會被抓出「判成重量不符」
    - 錄音存檔 → --from-data 讀回來重算，結果一樣
    - 用假的 UART 把整個互動錄音流程跑一遍

跑法（在專案根目錄）：
    python3 tests/test_calibrate_weight.py
"""
import builtins
import math
import queue
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging  # noqa: E402

logging.disable(logging.WARNING)   # 狀態機重播時的警告訊息不印

from core.uart_recording import Recording, Sample, Segment, load  # noqa: E402
from core.weight_calibration import calibrate, fit_line, replay_push, static_points, to_grams  # noqa: E402

FAILURES = []
A, B = 84210.0, 21.5          # 真正的 offset 與 scale（count/g）
SM = {"weight_match_timeout_sec": 5.0, "unscanned_change_timeout_sec": 8.0}


def check(cond, label, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}  {'' if cond else extra}")
    if not cond:
        FAILURES.append(label)


class Gen:
    def __init__(self, seed=0, noise_g=0.8, nonlin=0.0):
        self.rnd = random.Random(seed)
        self.t = 1000.0
        self.noise_g, self.nonlin = noise_g, nonlin
        self.segments, self.samples = [], {}

    def raw(self, g):
        g_eff = g + self.nonlin * (g / 1000.0) ** 2 * 1000.0
        return int(round(A + B * (g_eff + self.rnd.gauss(0, self.noise_g))))

    def seg(self, label, dur, wfun, **info):
        t0 = self.t
        out = []
        k = 0
        while k * 0.05 < dur:
            dt = k * 0.05
            out.append(Sample(t0 + dt, 0, 0, 100, 0.0, 0.0, 0.0, self.raw(wfun(dt))))
            k += 1
        self.t = t0 + dur + 2.0
        self.samples[label] = out
        self.segments.append(Segment(label, t0, t0 + dur, info))

    def rec(self):
        return Recording("weight_calibration", self.segments, self.samples, {})


def drop_curve(m, t_drop=1.2, overshoot=0.03):
    """放入：t_drop 秒時放下，第一筆就衝到 m(1+overshoot)，再以 5 Hz 衰減振盪回到 m。"""
    def w(dt):
        if dt < t_drop:
            return 0.0
        x = dt - t_drop
        return m + m * overshoot * math.exp(-x / 0.1) * math.cos(2 * math.pi * 5 * x)
    return w


def push_curve(seed, amp=12.0):
    rnd = random.Random(seed)
    bumps = [rnd.uniform(0, 20) for _ in range(12)]

    def w(dt):
        v = amp * 0.4 * math.sin(2 * math.pi * 3.1 * dt) * math.sin(2 * math.pi * 0.23 * dt)
        for b0 in bumps:   # 過地板接縫的衝擊
            if 0 <= dt - b0 < 0.3:
                v += amp * math.exp(-(dt - b0) * 12) * math.cos(2 * math.pi * 6 * (dt - b0))
        return v
    return w


def session(seed=0, masses=(500.0, 1000.0, 2000.0), drop_overshoot=0.03, push_amp=12.0, nonlin=0.0, drops=3):
    g = Gen(seed, nonlin=nonlin)
    g.seg("zero_start", 8, lambda dt: 0.0)
    for i, m in enumerate(masses, 1):
        g.seg(f"load_{i}", 8, lambda dt, m=m: m, mass_g=m)
    g.seg("zero_end", 8, lambda dt: 0.0)
    g.seg("push_empty", 20, push_curve(seed, push_amp))
    for k in range(1, drops + 1):
        g.seg(f"drop_{k}", 5, drop_curve(500.0, overshoot=drop_overshoot), mass_g=500.0)
    return g.rec()


# ----------------------------------------------------------------------
print("\n[1] 多點最小平方找回 offset、scale")
rec = session()
cal = calibrate(rec, SM)
check(cal.error is None, "沒有錯誤", cal.error)
check(abs(cal.offset - A) < 3 * B and abs(cal.scale - B) / B < 0.002, "a、b 跟真值相同（誤差 < 0.2%）",
      (cal.offset, cal.scale))
check(abs(cal.sigma_static_g - 0.8) < 0.2, "靜止雜訊 σ_s ≈ 0.8 g", cal.sigma_static_g)
check(max(abs(p.residual_g) for p in cal.points) < 0.5, "線性時殘差都很小")
check(abs(cal.zero_drift_g) < 0.5, "沒有漂移時零點漂移 ≈ 0", cal.zero_drift_g)

print("\n[1b] 錄的時候手還壓著（2026-11-16 實測的狀況）：中位數不被拉偏")
press = session()
seg = next(x for x in press.segments if x.label == "load_1")
for smp in press.samples["load_1"]:
    if smp.t - seg.t0 < 3.5:          # 前 3.5 秒多壓了 75 g（超過丟掉的 1 秒）
        smp.hx711_raw += int(75 * B)
cp = calibrate(press, SM)
check(abs(cp.scale - B) / B < 0.003 and cp.sigma_static_g < 1.5, "scale 與 σ_s 幾乎不受影響", (cp.scale, cp.sigma_static_g))
check(any("還在變" in s for s in cp.issues), "提示這一段還沒放穩", cp.issues)

print("\n[2] 未掃碼門檻：用狀態機重播推車震動")
push = rec.get("push_empty")
gr = to_grams(push, cal.offset, cal.scale)
ts = [s.t for s in push]
row0 = cal.settle_table[0]          # W_s = 0：每一筆都算，看得出推車震動
check(row0["t_min"] is not None and replay_push(ts, gr, row0["t_min"], SM) == 0, f"T = {row0['t_min']} g 時推車不誤報")
check(replay_push(ts, gr, row0["t_min"] - 1, SM) > 0, f"T = {row0['t_min'] - 1} g 就會誤報（找到的是最小值）")
cfg_w = {**SM, "settle_window_sec": cal.settle_window_sec, "settle_band_g": cal.settle_band_g}
check(replay_push(ts, gr, cal.t_min, cfg_w) == 0 and cal.t_min <= row0["t_min"],
      "等穩定之後推車震動影響更小（選中的 W_s 下 T_min 不變大）", (cal.t_min, row0["t_min"]))
check(cal.t_rec == max(math.ceil(1.5 * cal.t_min), math.ceil(6 * cal.sigma_static_g), 10),
      "建議值 = max(⌈1.5 T_min⌉, ⌈6σ_s⌉, 10)", (cal.t_rec, cal.t_min))
quiet = calibrate(session(push_amp=0.2), SM)
check(quiet.t_rec == 10 and "人為下限" in quiet.t_reason, "推車幾乎不晃時取人為下限 10 g", (quiet.t_rec, quiet.t_reason))
calm = calibrate(session(push_amp=4.0), SM)
check(calm.settle_table[0]["t_min"] < row0["t_min"], "推車越晃，門檻越大（W_s = 0 時比較）",
      (calm.settle_table[0]["t_min"], row0["t_min"]))

print("\n[3] 放入測試")
check(len(cal.drops) == 3 and all(d.outcome == "正常加入" for d in cal.drops), "正常放入 → 正常加入",
      [d.outcome for d in cal.drops])
check(cal.verdict == "可以", "判讀為可以", (cal.verdict, cal.issues))
hard = calibrate(session(drop_overshoot=0.8), SM)
row0 = hard.settle_table[0]
check(row0["window"] == 0 and any(d.outcome == "判成重量不符" for d in row0["drops"]),
      "衝擊很大時，不等穩定（舊版行為）會判成重量不符", [(d.outcome, round(d.overshoot_g)) for d in row0["drops"]])
check(hard.settle_window_sec and hard.settle_window_sec > 0 and all(d.outcome == "正常加入" and not d.extra_alerts
                                                                     for d in hard.drops),
      "等穩定之後每一趟都正常加入，挑出最小可行的 W_s", (hard.settle_window_sec, [d.outcome for d in hard.drops]))
check(cal.settle_window_sec == 0.2, "衝擊很小時 W_s = 0 就過，再多一格餘裕取 0.2 秒", cal.settle_window_sec)

print("\n[4] 線性與砝碼數")
nl = calibrate(session(nonlin=0.02), SM)
check(any("線性殘差" in s for s in nl.issues), "非線性時提示線性殘差", nl.issues)
one = calibrate(session(masses=(1000.0,)), SM)
check(one.verdict == "勉強" and any("只有一種砝碼" in s for s in one.issues), "只有一個砝碼 → 勉強", one.verdict)
none = calibrate(session(masses=()), SM)
check(none.error is not None, "沒有砝碼 → 報錯", none.error)
light = calibrate(rec, SM, {"口香糖": 3.0, "礦泉水": 605.0})
check(light.light_products == ["口香糖（3 g）"], "列出比門檻還輕的商品", light.light_products)

print("\n[5] 存檔 → 讀回來重算一樣（--from-data）")
with tempfile.TemporaryDirectory() as d:
    import csv
    import json
    p = Path(d) / "samples.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["segment", "epoch", "dx", "dy", "squal", "yaw_deg", "pitch_deg", "roll_deg", "hx711_raw"])
        for seg in rec.segments:
            for s in rec.get(seg.label):
                w.writerow([seg.label, f"{s.t:.6f}", 0, 0, 100, "0.000", "0.000", "0.000", s.hx711_raw])
    p.with_suffix(".meta.json").write_text(json.dumps(
        {"kind": "weight_calibration", "segments": [[s.label, s.t0, s.t1, s.info] for s in rec.segments]}))
    cal2 = calibrate(load(Path(d)), SM)
    check(abs(cal2.offset - cal.offset) < 1e-6 and abs(cal2.scale - cal.scale) < 1e-9 and cal2.t_rec == cal.t_rec,
          "讀回來重算的參數完全相同", (cal2.offset, cal2.scale, cal2.t_rec))

print("\n[6b] 狀態機的「等讀數穩定」本身（時間戳記直接給）")
from core.cart_manager import CartManager  # noqa: E402
from core.cart_state_machine import (  # noqa: E402
    ALERT_UNSCANNED_WEIGHT_CHANGE, CartStateMachine, GateEntryDetected, ItemScanned, LoginScanned,
    WeightSampleReceived,
)
from database.db_manager import DBManager, Member, Product  # noqa: E402


def run_sm(window, samples, band=2.0):
    with tempfile.TemporaryDirectory() as d:
        db = DBManager(Path(d) / "x.db")
        db.init_db(seed=False)
        db.upsert_member(Member("MEMBER-1", "x"))
        db.upsert_product(Product("P", "x", 1.0, 200.0, 20.0))
        sm = CartStateMachine(db=db, cart=CartManager(), config={
            "login_barcode_prefix": "MEMBER-", "unscanned_change_threshold_g": 10.0,
            "settle_window_sec": window, "settle_band_g": band})
        sm.process_event(LoginScanned(member_id="MEMBER-1", timestamp=0.0))
        sm.process_event(GateEntryDetected(timestamp=0.0))
        for i in range(10):
            sm.process_event(WeightSampleReceived(grams=0.0, timestamp=i * 0.05))
        sm.process_event(ItemScanned(barcode="P", timestamp=0.5))
        for t, g in samples:
            sm.process_event(WeightSampleReceived(grams=g, timestamp=t))
        n_alerts = sum(1 for a in sm.get_alerts() if a.code == ALERT_UNSCANNED_WEIGHT_CHANGE)
        return sm.cart.item_count(), n_alerts, sm.get_session().state


# 放進去：先 185 g（剛好在 200±20 內），晃到 215，再慢慢穩定在 199
trace = [(0.55 + i * 0.05, g) for i, g in enumerate([60, 185, 215, 190, 205, 196, 201] + [199.0] * 20)]
n0, a0, _ = run_sm(0.0, trace)
n1, a1, st1 = run_sm(0.3, trace)
check(n0 == 1 and a0 >= 1, "不等穩定：185 g 那一筆就加入，之後晃到 215 又誤報未掃碼", (n0, a0))
check(n1 == 1 and a1 == 0 and st1 == "shopping", "等 0.3 秒穩定：用穩定值 199 g 加入，沒有誤報", (n1, a1, st1))
bump = [(0.55 + i * 0.05, g) for i, g in enumerate([0, 40, 80, 30, 0] + [0.0] * 10)]
_, a2, st2 = run_sm(0.3, bump)
check(a2 == 0 and st2 == "awaiting_weight_increase", "碰一下籃子（沒穩定）不會被當成重量變化", (a2, st2))

print("\n[6] 用假的 UART 把互動錄音流程跑一遍")
import tools.calibrate_weight as tool  # noqa: E402
from drivers.uart_receiver import UartPacket  # noqa: E402


class FakeUart:
    def __init__(self):
        self.out_queue = queue.Queue(maxsize=200)
        self.g = 0.0
        self.rnd = random.Random(3)
        self.stop_ev = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop_ev.is_set():
            raw = int(round(A + B * (self.g + self.rnd.gauss(0, 0.8))))
            p = UartPacket(0, 0, 100, 0.0, 0.0, 0.0, raw, time.time())
            try:
                self.out_queue.put_nowait(p)
            except queue.Full:
                pass
            time.sleep(0.05)


fake = FakeUart()
answers = iter([
    ("", None),            # [1] 空秤
    ("500", None), ("", 500.0),   # 第 1 次 500 g → Enter 後放上
    ("1500", None), ("", 1500.0),
    ("", None),            # 結束放砝碼
    ("", 0.0),             # [3] 拿掉
    ("", None),            # [4] 推車
    ("300", None),         # [5] 放入用 300 g
    ("", None), ("", 0.0), ("", None),
])


def fake_input(prompt=""):
    ans, g = next(answers)
    if g is not None:
        fake.g = g
    if "看到「放！」" in prompt:
        threading.Timer(1.3, lambda: setattr(fake, "g", 300.0)).start()
    return ans


real_input = builtins.input
builtins.input = fake_input
try:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "s"
        tap = tool.record(fake, out, static_sec=2.0, push_sec=2.0, drops=2)
        fake.stop_ev.set()
        r = load(out)
        labels = [s.label for s in r.segments]
        check(labels == ["zero_start", "load_1", "load_2", "zero_end", "push_empty", "drop_1", "drop_2"],
              "段落順序正確", labels)
        check([s.info.get("mass_g") for s in r.segments if s.label.startswith("load")] == [500.0, 1500.0],
              "meta 記下每次砝碼重量")
        pts = static_points(r)
        a, b = fit_line(pts)
        check(abs(b - B) / B < 0.01, "從假 UART 錄到的資料算回正確的 scale", b)
        cal6 = calibrate(r, SM)
        check(cal6.drops and all(d.outcome == "正常加入" for d in cal6.drops), "假 UART 的放入測試判成正常加入",
              [(d.outcome, d.detail) for d in cal6.drops])
finally:
    builtins.input = real_input
    fake.stop_ev.set()

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("全部通過")
