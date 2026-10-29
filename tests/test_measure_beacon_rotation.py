"""tools.measure_beacon_rotation 的計算邏輯測試——不需要 BLE 硬體。

測 summarize_step()（兩顆逐格點對齊算差分）、rank_angles()（依最差間隔排序）、
symmetry_warnings()（0° 與 180° 不一致時提醒）。

跑法（在專案根目錄）：
    python3 tests/test_measure_beacon_rotation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.measure_beacon_rotation import rank_angles, summarize_step, symmetry_warnings  # noqa: E402

FAILURES = []


def check(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {extra}")
        FAILURES.append(label)


T0, DUR = 1000.0, 15.0
WIN, MIN_N, METHOD = 1.0, 3, "p75"


def steady(level, rate_hz=8.0, phase=0.0):
    n = int(DUR * rate_hz)
    return [(T0 + phase + i / rate_hz, level) for i in range(n)]


def step(inside_level, outside_level):
    return summarize_step(T0, T0 + DUR, steady(inside_level), steady(outside_level, phase=0.03),
                          WIN, MIN_N, METHOD)


print("\n[1] 差分 = 內側 − 外側，逐格點對齊")
s = step(-50, -80)
check(s["diff"] is not None and abs(s["diff"]["mean"] - 30.0) < 1e-6, "差分平均 +30", s["diff"])
check(s["diff"]["valid_ratio"] > 0.99, "兩顆都穩定時幾乎每個格點都能算差分", s["diff"])

print("\n[2] 其中一顆沒資料時差分給 None")
s2 = summarize_step(T0, T0 + DUR, steady(-50), [], WIN, MIN_N, METHOD)
check(s2["diff"] is None, "外側完全沒收到 → 差分 None", s2["diff"])

print("\n[3] 依最差間隔排序，選出極化對齊的角度")
# 假設 90° 對齊：正面最強；0° 偏 75°，正面掉 11.7 dB
sweep_in = {"0": step(-60, -80), "90": step(-48, -80)}   # 推車在門內：內側正面、外側背面
sweep_out = {"0": step(-70, -70), "90": step(-70, -58)}  # 推車在門外：內側背面、外側正面
rank = rank_angles(sweep_in, sweep_out)
check(rank[0]["angle"] == "90", "90° 排第一", rank)
check(abs(rank[0]["swing_db"] - 44.0) < 1e-6, "90° 變化幅度 = 32 − (−12) = 44 dB", rank[0])
check(abs(rank[1]["swing_db"] - 20.0) < 1e-6, "0° 變化幅度 = 20 − 0 = 20 dB", rank[1])

print("\n[4] 只有一輪量到的角度不列入比較")
rank2 = rank_angles({"0": step(-60, -80), "45": step(-55, -80)}, {"0": step(-70, -70)})
check([r["angle"] for r in rank2] == ["0"], "45° 只量了第 1 輪 → 不比較", rank2)

print("\n[5] 0° 與 180° 不一致時提醒")
ok = {"0": step(-50, -80), "180": step(-51, -80)}
bad = {"0": step(-50, -80), "180": step(-58, -80)}
check(symmetry_warnings(ok, "inside") == [], "差 1 dB 不提醒")
check(len(symmetry_warnings(bad, "inside")) == 1, "差 8 dB 要提醒")

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("全部通過")
