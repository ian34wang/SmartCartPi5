"""core/positioning/track.py 與 tools/track_odometry.py 的純邏輯測試（不需要硬體）。

合成封包：先決定車子每一步在車身座標走多少 (f, l)，再用 core/positioning/odometry.sensor_to_cart 的反函數換成
PMW3901 的 (dx, dy)：
    s_x = cos φ · f − sin φ · l,    s_y = sin φ · f + cos φ · l,    dx = s_x,  dy = m · s_y（m = −1 鏡像）
（f, l 單位是 count = mm / px_to_mm；小數的部分累積到下一步，總量不失真。）

跑法（在專案根目錄）：
    python3 tests/test_odometry_track.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.positioning import track as ot  # noqa: E402
from core.recording import Sample  # noqa: E402
from tools.track_odometry import parse_float, parse_pair  # noqa: E402

FAILURES = []


def check(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {extra}")
        FAILURES.append(label)


ODO = {"optical_flow_px_to_mm": 0.5, "flow_rotation_deg": 30.0, "flow_mirror": True, "yaw_sign": 1}


class Synth:
    """產生合成封包。yaw 是 BNO080 送上來的值（yaw_sign = +1，所以就是里程計航向）。"""

    def __init__(self, yaw0: float, odo=ODO):
        self.t, self.yaw, self.odo = 1000.0, yaw0, odo
        self.rx = self.ry = 0.0
        self.out = []

    def _emit(self, dx, dy, squal=120):
        self.t += 0.05
        y = (self.yaw + 180.0) % 360.0 - 180.0          # BNO080 送 (−180, 180]
        self.out.append(Sample(self.t, dx, dy, squal, y, 0.0, 0.0, 0))

    def go(self, f_m, l_m, steps=50, squal=120):
        k = self.odo["optical_flow_px_to_mm"]
        phi = math.radians(self.odo["flow_rotation_deg"])
        m = -1 if self.odo["flow_mirror"] else 1
        f, l = 1000.0 * f_m / k / steps, 1000.0 * l_m / k / steps
        for _ in range(steps):
            sx = math.cos(phi) * f - math.sin(phi) * l + self.rx
            sy = math.sin(phi) * f + math.cos(phi) * l + self.ry
            dx, dyv = round(sx), round(sy)
            self.rx, self.ry = sx - dx, sy - dyv
            self._emit(dx, m * dyv, squal)

    def turn(self, deg, steps=20):
        for _ in range(steps):
            self.yaw += deg / steps
            self._emit(0, 0)


print("\n[1] 正方形：每邊 1 m、左轉 4 次，起點航向 40°（圖上一律起點朝上）")
s = Synth(40.0)
s.go(1.0, 0)
tr1 = ot.build_track(s.out, ODO)
f1, l1 = tr1.start_frame()[-1]
check(abs(f1 - 1.0) < 0.01 and abs(l1) < 0.01, "第一邊：前方 1 m（起點座標，跟開機航向無關）", (f1, l1))
s.turn(90)
s.go(1.0, 0)
f2, l2 = ot.build_track(s.out, ODO).start_frame()[-1]
check(abs(f2 - 1.0) < 0.01 and abs(l2 - 1.0) < 0.01, "左轉後再走 1 m：(1, 1)", (f2, l2))
s.turn(90)
s.go(1.0, 0)
s.turn(90)
s.go(1.0, 0)
s.turn(90)
tr = ot.build_track(s.out, ODO)
m = ot.metrics(tr, true_f_m=0.0, true_l_m=0.0, true_turn_deg=360.0)
check(abs(m["path_m"] - 4.0) < 0.02, "路徑長 L = 4 m", m["path_m"])
check(m["end_err_m"] < 0.02 and m["displacement_m"] < 0.02, "回到起點：閉合誤差 ≈ 0", m["end_err_m"])
check(abs(m["turn_deg"] - 360.0) < 1e-6 and abs(m["turn_err_deg"]) < 1e-6, "總轉角 +360°（跨過 ±180° 也接得起來）", m["turn_deg"])
check(m["gaps"] == 0 and m["skipped_low_squal"] == 0 and m["squal_median"] == 120, "沒有掉資料、沒有跳過")

print("\n[2] 側推（車身左方）與錯的實際值")
s = Synth(-120.0)
s.go(0, 0.8)
tr = ot.build_track(s.out, ODO)
m = ot.metrics(tr, true_f_m=0.0, true_l_m=1.0, true_distance_m=0.8)
check(abs(m["end_l_m"] - 0.8) < 0.01 and abs(m["end_f_m"]) < 0.01, "往左推 0.8 m → l = +0.8", (m["end_f_m"], m["end_l_m"]))
check(abs(m["end_err_l_m"] + 0.2) < 0.01 and abs(m["end_err_pct_of_path"] - 25.0) < 1.5, "實際 1.0 m：左方差 −0.2 m、‖e‖/L ≈ 25%",
      (m["end_err_l_m"], m["end_err_pct_of_path"]))
check(abs(m["distance_err_m"]) < 0.01, "直線距離 e_D ≈ 0", m["distance_err_m"])
m0 = ot.metrics(tr)
check(m0["end_err_m"] is None and m0["turn_err_deg"] is None and m0["distance_err_m"] is None, "沒給實際值：誤差是 None（不補值）")

print("\n[3] 跟 app 一樣的 OdometryEngine：低 squal、掉資料")
s = Synth(0.0)
s.go(0.5, 0, steps=20, squal=10)
s.t += 0.5                                  # 中間停 0.5 s 沒封包
s.go(0.5, 0, steps=20)
m = ot.metrics(ot.build_track(s.out, ODO, min_squal=30), true_f_m=1.0, true_l_m=0.0)
check(m["skipped_low_squal"] == 20 and abs(m["end_f_m"] - 0.5) < 0.01, "squal < min_squal 的那半段不累加", (m["skipped_low_squal"], m["end_f_m"]))
check(m["gaps"] == 1 and m["max_gap_s"] > 0.5, "掉資料算到 1 次", (m["gaps"], m["max_gap_s"]))
m_app = ot.metrics(ot.build_track(s.out, ODO))
check(abs(m_app["end_f_m"] - 1.0) < 0.01, "min_squal = 0（app 的設定）全部累加", m_app["end_f_m"])

print("\n[4] 錄音 info → 實際值")
check(ot.truth_from_info({"true_f_m": 3, "true_l_m": 0, "true_turn_deg": -90}) ==
      {"true_f_m": 3.0, "true_l_m": 0.0, "true_turn_deg": -90.0}, "track_odometry 存的欄位")
check(ot.truth_from_info({"distance_mm": 5000.0, "angle_deg": 90}) == {"true_distance_m": 5.0, "true_turn_deg": 90.0},
      "光流校正錄音的欄位")
check(ot.truth_from_info({"true_f_m": 3}) == {}, "只有前方沒有左方：不算終點誤差")

print("\n[5] 輸入解析")
check(parse_pair("3,0") == (3.0, 0.0) and parse_pair(" 1.2  -0.5 ") == (1.2, -0.5) and parse_pair("1，2") == (1.0, 2.0),
      "逗號、空白、全形逗號都可以")
check(parse_pair("") is None and parse_pair("3") is None and parse_pair("a,b") is None, "看不懂就跳過")
check(parse_float("-90") == -90.0 and parse_float("") is None, "轉角")

print("\n[6] 畫圖")
img = ot.render(tr1.start_frame(), (500, 400), 0.3, lines=ot.summary_lines("run1", ot.metrics(tr1)),
                truth_end=(1.0, 0.0), heading_deg=0.0)
check(img.shape == (400, 500, 3), "圖的大小", img.shape)
check(ot.render([], (300, 300)).shape == (300, 300, 3), "還沒有資料也畫得出來（等待畫面）")
check(ot.summary_lines("x", ot.metrics(ot.Track()))[0] == "x: no data", "空的一趟")

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("全部通過")
