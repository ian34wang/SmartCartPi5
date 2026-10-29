"""tools.measure_beacon_shield 的統計邏輯測試——不需要 BLE 硬體。

只測 analyze()/compare() 這兩個純計算函式：封包數、到達率、斷訊、可判定時間
比例算得對不對。實際收封包那段要有真的 Beacon 才能測。

跑法（在專案根目錄）：
    python3 tests/test_measure_beacon_shield.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.measure_beacon_shield import analyze, compare  # noqa: E402

FAILURES = []


def check(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {extra}")
        FAILURES.append(label)


T0 = 1000.0
DUR = 30.0
WIN, MIN_N, METHOD = 0.4, 3, "p75"

print("\n[1] 穩定 10 包/秒、沒有斷訊")
steady = [(T0 + 0.05 + 0.1 * i, -60 + (i % 3) - 1) for i in range(300)]
a = analyze(T0, T0 + DUR, steady, WIN, MIN_N, METHOD)
check(a["packets"] == 300, "封包數 300", a["packets"])
check(abs(a["rate_pps"] - 10.0) < 0.01, "到達率 10 包/秒", a["rate_pps"])
check(a["gaps"]["dropouts"] == 0, "沒有 >1 秒的斷訊", a["gaps"])
check(abs(a["gaps"]["median_ms"] - 100) < 1, "封包間隔中位數 100 ms", a["gaps"]["median_ms"])
check(a["judgeable_ratio"] > 0.99, "幾乎整段都能判定", a["judgeable_ratio"])
check(a["rssi"]["max"] == -59 and a["rssi"]["min"] == -61, "最大/最小值", a["rssi"])
check(a["repeat_ratio"] == 0.0, "-61,-60,-59 循環，沒有連續相同的值", a["repeat_ratio"])

print("\n[2] 有兩段斷訊（2 秒、1.5 秒），收到的那些 RSSI 比較低")
gappy = [(t, -70) for t, _ in steady
         if not (10.0 <= t - T0 < 12.0) and not (20.0 <= t - T0 < 21.5)]
b = analyze(T0, T0 + DUR, gappy, WIN, MIN_N, METHOD)
check(b["packets"] == 300 - 20 - 15, "封包數扣掉斷訊那段", b["packets"])
check(b["gaps"]["dropouts"] == 2, "抓到 2 次斷訊", b["gaps"])
check(abs(b["gaps"]["max_ms"] - 2100) < 1, "最長斷訊約 2.1 秒（前後封包之間）", b["gaps"]["max_ms"])
expected_lost = (2.0 + 1.5) / DUR
check(abs((a["judgeable_ratio"] - b["judgeable_ratio"]) - expected_lost) < 0.03,
      "可判定時間少掉的比例 ≈ 斷訊時間比例", (a["judgeable_ratio"], b["judgeable_ratio"]))
check(b["repeat_ratio"] == 1.0, "全部都是 -70 → 連續相同比例 100%", b["repeat_ratio"])

print("\n[3] 開頭很久才收到第一包也算斷訊")
late = [(T0 + 5.0 + 0.1 * i, -65) for i in range(250)]
c = analyze(T0, T0 + DUR, late, WIN, MIN_N, METHOD)
check(c["gaps"]["dropouts"] == 1 and c["gaps"]["max_ms"] >= 5000, "前 5 秒沒封包算一次斷訊", c["gaps"])

print("\n[4] 完全沒收到")
d = analyze(T0, T0 + DUR, [], WIN, MIN_N, METHOD)
check(d["packets"] == 0 and d["rssi"] is None and d["judgeable_ratio"] == 0.0, "空資料不會當掉", d)

print("\n[5] 比較 A − B")
cmp_ab = compare(a, b)
check(abs(cmp_ab["rssi_mean_db"] - 10.0) < 0.5, "平均差約 10 dB", cmp_ab)
check(cmp_ab["rate_ratio"] is not None and cmp_ab["rate_ratio"] > 1.0, "到達率比 > 1", cmp_ab)
cmp_ad = compare(a, d)
check(cmp_ad["rssi_mean_db"] is None and cmp_ad["rate_ratio"] is None, "對方沒資料時給 None", cmp_ad)

print("\n[6] 範圍外的封包不算")
outside = steady + [(T0 - 1.0, -40), (T0 + DUR + 1.0, -40)]
e = analyze(T0, T0 + DUR, outside, WIN, MIN_N, METHOD)
check(e["packets"] == 300 and e["rssi"]["max"] == -59, "量測區間外的封包被排除", e["packets"])

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("全部通過")
