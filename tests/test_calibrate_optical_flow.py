"""光流與航向校正（core/positioning/flow_calibration.py、tools/calibrate_optical_flow.py）與
core/positioning/odometry.py 座標轉換的測試——不需要硬體。

用合成封包（已知 k、安裝角 φ、鏡像 m、yaw 正負號）驗證：
    - 找回正確的 k、φ、m、yaw_sign
    - 用找回的參數跑 OdometryEngine，直推的距離與方向都對
    - 沒做側推時假設不鏡像並提示；直推不直、k 不穩時提示
    - OdometryEngine 不設新參數時跟舊版行為一樣
    - 存檔 → --from-data 讀回來一樣；用假的 UART 把互動錄音跑一遍

跑法（在專案根目錄）：
    python3 tests/test_calibrate_optical_flow.py
"""
import builtins
import csv
import json
import math
import queue
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.positioning.flow_calibration import calibrate  # noqa: E402
from core.positioning.odometry import OdometryEngine, rotate_local_to_global, sensor_to_cart  # noqa: E402
from core.recording import Recording, Sample, Segment, load  # noqa: E402
from drivers.uart_receiver import UartPacket  # noqa: E402

FAILURES = []


def check(cond, label, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}  {'' if cond else extra}")
    if not cond:
        FAILURES.append(label)


class Cart:
    """真值：k（mm/count）、φ（安裝角）、m（鏡像）、ys（yaw 正負號）。"""

    def __init__(self, k=0.42, phi=23.0, mirror=True, ys=-1, seed=0):
        self.k, self.phi, self.m, self.ys = k, phi, (-1 if mirror else 1), ys
        self.rnd = random.Random(seed)
        self.t = 1000.0
        self.yaw = 37.0          # 真實航向（逆時針為正）
        self.acc = [0.0, 0.0]
        self.segments, self.samples = [], {}

    def _sensor(self, f_mm, l_mm):
        cf, cl = f_mm / self.k, l_mm / self.k             # 車身座標的 count
        p = math.radians(self.phi)
        sx = math.cos(p) * cf - math.sin(p) * cl          # s = M R(φ) c
        sy = math.sin(p) * cf + math.cos(p) * cl
        sy *= self.m
        self.acc[0] += sx
        self.acc[1] += sy
        ix, iy = int(round(self.acc[0])), int(round(self.acc[1]))
        self.acc[0] -= ix
        self.acc[1] -= iy
        return ix, iy

    def seg(self, label, dur, f_rate=0.0, l_rate=0.0, yaw_rate=0.0, wobble=0.0, info=None, outliers=0, still=0.0):
        out = []
        t0 = self.t
        n = int(dur / 0.05)
        bad = set(self.rnd.sample(range(n), outliers)) if outliers else set()
        for i in range(n):
            dt = 0.05
            f = f_rate * dt * (1 + self.rnd.gauss(0, 0.05))
            l = l_rate * dt + wobble * math.sin(i * 0.3) * dt
            if still <= i * dt <= dur - still:   # 頭尾停著不動（按 Enter 前後）
                self.yaw += yaw_rate * dt
            dx, dy = self._sensor(f, l)
            if i in bad:
                dx, dy = dx + 400, dy - 300
            raw_yaw = self.ys * self.yaw + self.rnd.gauss(0, 0.05)
            raw_yaw = (raw_yaw + 180) % 360 - 180
            out.append(Sample(t0 + i * dt, dx, dy, 120 + self.rnd.randint(-10, 10), raw_yaw, 0.0, 0.0, 84000))
        self.t = t0 + dur + 3.0
        self.samples[label] = out
        self.segments.append(Segment(label, t0, t0 + dur, info or {}))

    def rec(self):
        return Recording("flow_calibration", self.segments, self.samples, {})


def session(lateral=True, runs=3, **kw):
    c = Cart(**{k: v for k, v in kw.items() if k in ("k", "phi", "mirror", "ys", "seed")})
    D = 3000.0
    c.seg("static", 5)
    for i in range(1, runs + 1):
        speed = 600.0 + 100 * i
        c.seg(f"fwd_{i}", D / speed, f_rate=speed, wobble=kw.get("wobble", 0.0), info={"distance_mm": D},
              outliers=kw.get("outliers", 0) if i == 1 else 0)
    if lateral:
        c.seg("left_1", 2.0, l_rate=300.0)
    c.seg("turn_left", 4.4, yaw_rate=30.0, still=0.7, info={"angle_deg": 90.0})
    return c.rec()


# ----------------------------------------------------------------------
print("\n[1] OdometryEngine 座標轉換")
check(sensor_to_cart(10, 3) == (10, 3), "不設參數時 = 舊版（感測器軸就是車身軸）")
f, l = sensor_to_cart(math.cos(math.radians(30)), math.sin(math.radians(30)), 30.0, False)
check(abs(f - 1) < 1e-9 and abs(l) < 1e-9, "安裝角 30°：沿 30° 的位移 = 正前方")
f, l = sensor_to_cart(0, -1, 0.0, True)
check(abs(f) < 1e-9 and abs(l - 1) < 1e-9, "鏡像：感測器 −y = 車身左方")
gx, gy = rotate_local_to_global(1.0, 0.0, 90.0)
check(abs(gx) < 1e-9 and abs(gy - 1) < 1e-9, "航向 90° 時往前 = 全域 +Y（逆時針為正）")
e = OdometryEngine.from_config({"optical_flow_px_to_mm": 0.5, "flow_rotation_deg": 10, "flow_mirror": True, "yaw_sign": -1})
check((e.px_to_mm, e.rotation_deg, e.mirror, e.yaw_sign) == (0.5, 10.0, True, -1), "from_config 讀到全部參數")
e0 = OdometryEngine.from_config({})
check((e0.px_to_mm, e0.rotation_deg, e0.mirror, e0.yaw_sign) == (1.0, 0.0, False, 1), "config 沒有新欄位時跟舊版一樣")

print("\n[2] 找回 k、φ、m、yaw_sign，重播距離與方向正確")
for truth in ({"k": 0.42, "phi": 23.0, "mirror": True, "ys": -1},
              {"k": 0.18, "phi": -95.0, "mirror": False, "ys": 1}):
    cal = calibrate(session(**truth))
    tag = f"k={truth['k']} φ={truth['phi']} 鏡像={truth['mirror']} ys={truth['ys']}"
    check(cal.error is None and abs(cal.px_to_mm - truth["k"]) / truth["k"] < 0.01, f"{tag}：k 誤差 < 1%",
          cal.px_to_mm)
    check(abs((cal.rotation_deg - truth["phi"] + 180) % 360 - 180) < 1.0, f"{tag}：φ 誤差 < 1°", cal.rotation_deg)
    check(cal.mirror == truth["mirror"] and cal.yaw_sign == truth["ys"], f"{tag}：鏡像與 yaw 正負號正確",
          (cal.mirror, cal.yaw_sign))
    check(all(abs(r.replay_err_pct) < 1.0 and abs(r.replay_heading_err_deg) < 1.0 for r in cal.runs),
          f"{tag}：OdometryEngine 重播每趟距離 < 1%、方向 < 1°",
          [(round(r.replay_err_pct, 2), round(r.replay_heading_err_deg, 2)) for r in cal.runs])
    check(abs(cal.lateral.replay_heading_err_deg) < 2.0, f"{tag}：側推重播在航向 +90°",
          cal.lateral.replay_heading_err_deg)
    check(cal.verdict == "可以", f"{tag}：判讀為可以", (cal.verdict, cal.issues))

print("\n[3] 資料不完整或不好時的提示")
nolat = calibrate(session(lateral=False, mirror=False))
check(nolat.mirror is None and any("沒有做側推" in s for s in nolat.issues) and nolat.verdict == "勉強",
      "沒做側推 → 假設不鏡像、勉強", (nolat.mirror, nolat.verdict))
bad_mirror = calibrate(session(lateral=False, mirror=True))
check(any(abs(r.replay_err_pct) < 1 for r in bad_mirror.runs), "沒側推時直推距離仍然正確（鏡像只影響左右）")
one = calibrate(session(runs=1))
check(any("只推了一趟" in s for s in one.issues), "只推一趟 → 提示看不出重複性")
out = calibrate(session(outliers=3))
check(any("位移異常大" in s for s in out.issues), "有異常大的位移 → 提示（執行期不濾）", out.issues)
check(calibrate(Recording("x", [], {}, {})).error is not None, "沒有直推資料 → 報錯")

print("\n[4] 存檔 → 讀回來重算一樣")
rec = session()
cal = calibrate(rec)
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "samples.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["segment", "epoch", "dx", "dy", "squal", "yaw_deg", "pitch_deg", "roll_deg", "hx711_raw"])
        for seg in rec.segments:
            for s in rec.get(seg.label):
                w.writerow([seg.label, f"{s.t:.6f}", s.dx, s.dy, s.squal, f"{s.yaw_deg:.3f}", "0", "0", s.hx711_raw])
    p.with_suffix(".meta.json").write_text(json.dumps(
        {"segments": [[s.label, s.t0, s.t1, s.info] for s in rec.segments]}))
    cal2 = calibrate(load(Path(d)))
    check(abs(cal2.px_to_mm - cal.px_to_mm) < 1e-9 and abs(cal2.rotation_deg - cal.rotation_deg) < 0.01
          and cal2.mirror == cal.mirror and cal2.yaw_sign == cal.yaw_sign, "讀回來重算一樣")

print("\n[5] 用假的 UART 把互動錄音流程跑一遍")
import tools.calibrate_optical_flow as tool  # noqa: E402


class FakeUart:
    def __init__(self):
        self.out_queue = queue.Queue(maxsize=200)
        self.cart = Cart(k=0.3, phi=40.0, mirror=False, ys=1)
        self.f_rate = self.l_rate = self.yaw_rate = 0.0
        self.stop_ev = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop_ev.is_set():
            c = self.cart
            c.yaw += self.yaw_rate * 0.05
            dx, dy = c._sensor(self.f_rate * 0.05, self.l_rate * 0.05)
            try:
                self.out_queue.put_nowait(UartPacket(dx, dy, 120, (c.yaw + 180) % 360 - 180, 0.0, 0.0, 84000, time.time()))
            except queue.Full:
                pass
            time.sleep(0.05)


fake = FakeUart()


def go(**rates):
    def f():
        for k, v in rates.items():
            setattr(fake, k, v)
    return f


# (回答, 回答前先等幾秒, 回答時把推車改成什麼動作)：開始錄的 Enter 立刻回、
# 停止錄的 Enter 先等 2 秒（500 mm/s × 2 s = 1000 mm；45°/s × 2 s = 90°）
steps = iter([
    ("", 0, None),                                # [1] 靜止
    ("100", 0, None),                             # 距離 100 cm
    ("", 0, go(f_rate=500.0)), ("", 2, go(f_rate=0.0)),   # 第 1 趟
    ("", 0, None),                                # 拉回起點
    ("", 0, go(f_rate=500.0)), ("", 2, go(f_rate=0.0)),   # 第 2 趟
    ("", 0, go(l_rate=250.0)), ("", 2, go(l_rate=0.0)),   # 側推
    ("", 0, go(yaw_rate=45.0)), ("", 2, go(yaw_rate=0.0)),  # 左轉
])


def fake_input(prompt=""):
    ans, wait, act = next(steps)
    time.sleep(wait)
    if act is not None:
        act()
    return ans


real_input = builtins.input
builtins.input = fake_input
try:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "s"
        tap = tool.record(fake, out, runs=2, static_sec=1.0)
        fake.stop_ev.set()
        r = load(out)
        check([s.label for s in r.segments] == ["static", "fwd_1", "fwd_2", "left_1", "turn_left"], "段落正確",
              [s.label for s in r.segments])
        c5 = calibrate(r)
        check(abs(c5.rotation_deg - 40.0) < 3 and c5.mirror is False and c5.yaw_sign == 1,
              "從假 UART 錄到的資料找回安裝角、鏡像、yaw 正負號", (c5.rotation_deg, c5.mirror, c5.yaw_sign))
        check(abs(c5.px_to_mm - 0.3) / 0.3 < 0.1, "k 在 10% 內（實際推的時間由 sleep 控制，有誤差）", c5.px_to_mm)
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
