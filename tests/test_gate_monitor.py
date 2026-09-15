"""core.gate_monitor.GateMonitor 的邏輯測試——不需要 BLE 硬體。

測的是「把一連串 RSSI 觀測餵進去，什麼時候該判定成進場/出場」。真實 BLE 掃描
（drivers/ble_beacon_scanner.py）不在範圍內，那支要有真的 Beacon 才能測。

跑法（在專案根目錄）：
    python3 tests/test_gate_monitor.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.gate_monitor import GateMonitor
from core.landmark_correction import smooth_rssi

INSIDE = "GATE-INSIDE"
OUTSIDE = "GATE-OUTSIDE"
FAILURES = []


def check(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {extra}")
        FAILURES.append(label)


def new_monitor(**kw):
    opts = dict(
        inside_beacon_id=INSIDE, outside_beacon_id=OUTSIDE,
        smoothing_sec=0.4, smoothing_method="p75", min_samples=1,
        hysteresis_dbm=2.0,
        min_crossing_rssi_dbm=-65.0, min_confirm_samples=3,
    )
    opts.update(kw)
    return GateMonitor(**opts)


def feed(monitor, seq, t0=0.0):
    """seq 是 [(內側 RSSI, 外側 RSSI), ...]，回傳判定出來的事件列表。"""
    events = []
    t = t0
    for inside, outside in seq:
        t += 0.2
        for beacon_id, rssi in ((INSIDE, inside), (OUTSIDE, outside)):
            result = monitor.process_observation(beacon_id, rssi, t)
            if result:
                events.append(result)
    return events


# ----------------------------------------------------------------------
# 模擬一次真實的穿越。手寫幾筆固定數列沒辦法反映真實情況——真實情況是
# 20~50 Hz 的廣播、單邊的 deep fade 雜訊、以及推車以某個速度連續移動，
# 而濾波器的延遲只有在「連續移動」時才會顯現。
# ----------------------------------------------------------------------
import math
import random


def simulate_crossing(direction="entering", D=1.5, speed=1.0, rate_hz=20.0,
                      fade_prob=0.35, fade_db=6.0, tx_ref=-40.0, seed=1):
    """回傳 [(beacon_id, rssi, timestamp), ...]，模擬推車沿兩顆連線通過門口。

    D      兩顆 Beacon 的間距（公尺）
    speed  推車速度（m/s）
    fade_* 單邊的多路徑衰減：有 fade_prob 的機率這一筆掉 fade_db
    """
    rng = random.Random(seed)
    events = []
    span = 2.0                      # 從門外 1 m 走到門內 1 m
    duration = span / speed
    n = int(duration * rate_hz)
    for i in range(n):
        t = i / rate_hz
        x = -1.0 + speed * t        # -1 = 門外 1 m，+1 = 門內 1 m
        if direction == "exiting":
            x = -x
        d_in = abs(D / 2 - x) + 0.05
        d_out = abs(D / 2 + x) + 0.05
        for bid, d in ((INSIDE, d_in), (OUTSIDE, d_out)):
            rssi = tx_ref - 20 * math.log10(d)
            if rng.random() < fade_prob:
                rssi -= fade_db
            events.append((bid, round(rssi), t))
    return events


def replay(monitor, events):
    out = []
    for bid, rssi, t in events:
        r = monitor.process_observation(bid, rssi, t)
        if r:
            out.append((round(t, 2), r))
    return out


print("\n[1] 基本方向判定（模擬 20 Hz、含 deep fade 的真實穿越）")
m = new_monitor(min_confirm_samples=2, min_crossing_rssi_dbm=-70.0)
ev = replay(m, simulate_crossing("entering"))
check([r for _, r in ev] == ["entering"], "從外往內＝entering", ev)
m_x = new_monitor(min_confirm_samples=2, min_crossing_rssi_dbm=-70.0)
ev_x = replay(m_x, simulate_crossing("exiting"))
check([r for _, r in ev_x] == ["exiting"], "從內往外＝exiting", ev_x)

# 判定應該落在門檻線附近（t=1.0 是通過中線的時刻），而且延遲不超過一個窗口
if ev:
    delay = ev[0][0] - 1.0
    check(-0.1 <= delay <= 0.6, f"判定時間點落在通過中線之後 0~0.6 秒（實際 {delay:+.2f}s）", delay)

print("\n[1b] 濾波延遲：窗口越長判定越晚（這是取上包絡線的必然代價）")
delays = {}
for sec in (0.2, 0.4, 0.8):
    mm = new_monitor(smoothing_sec=sec, min_confirm_samples=2, min_crossing_rssi_dbm=-70.0)
    e = replay(mm, simulate_crossing("entering"))
    delays[sec] = (e[0][0] - 1.0) if e else None
print(f"       窗口 -> 延遲：{ {k: (round(v,2) if v is not None else None) for k,v in delays.items()} }")
check(all(v is not None for v in delays.values()), "各種窗口長度都判定得出來", delays)
check(delays[0.2] <= delays[0.8], "窗口越短判定越早", delays)

print("\n[2] 只有單邊有訊號時不判定")
m2 = new_monitor(min_confirm_samples=1)
lone = [m2.process_observation(INSIDE, -50, i * 0.1) for i in range(10)]
check(all(r is None for r in lone), "只收到內側 Beacon 不會誤判", lone)

print("\n[3] 訊號太弱（人在店裡別處，只是相對靠近某一邊）不該觸發")
# 車子在店裡別處閒逛：離兩顆都很遠，訊號都很弱，但其中一邊「相對」比較強。
# 這種情況絕對不能被判定成穿越門口。
m3 = new_monitor(min_crossing_rssi_dbm=-65.0, min_confirm_samples=2)
weak = []
for i in range(60):
    t = i * 0.02
    weak.append((INSIDE, -88 if i < 30 else -82, t))     # 後半段內側相對變強
    weak.append((OUTSIDE, -82 if i < 30 else -88, t))    # 訊號翻轉，但兩邊都 < -65
check(replay(m3, weak) == [], "兩邊都低於絕對門檻時，就算相對強弱翻轉也不判定")

print("\n[4] 單筆雜訊的瞬間翻轉會被連續樣本確認擋掉")
m4 = new_monitor(min_confirm_samples=3)
for i in range(20):                                  # 先穩定在內側
    m4.process_observation(INSIDE, -55, i * 0.02)
    m4.process_observation(OUTSIDE, -80, i * 0.02)
spike = [m4.process_observation(INSIDE, -80, 0.42),   # 只有一筆突然翻到外側
         m4.process_observation(OUTSIDE, -55, 0.42)]
check(all(r is None for r in spike), "單筆突波不算穿越", spike)

print("\n[5] 航向交叉驗證")
HEAD_OPTS = dict(min_confirm_samples=2, min_crossing_rssi_dbm=-70.0)

m5 = new_monitor(expected_exit_yaw_deg=0.0, heading_tolerance_deg=45.0, **HEAD_OPTS)
check(m5.heading_check_enabled, "有填 gate_exit_yaw_deg 時啟用")
m5.update_heading(90.0)                     # 跟進場預期的 180 度差太多
check(replay(m5, simulate_crossing("entering")) == [], "航向不符時擋下判定")
check(m5.rejected_by_heading_count >= 1, "有記錄被擋下的次數", m5.rejected_by_heading_count)

m6 = new_monitor(expected_exit_yaw_deg=0.0, heading_tolerance_deg=45.0, **HEAD_OPTS)
m6.update_heading(180.0)                    # 正好是進場方向
check([r for _, r in replay(m6, simulate_crossing("entering"))] == ["entering"],
      "航向相符時正常判定")

m7 = new_monitor(expected_exit_yaw_deg=None, **HEAD_OPTS)
check(not m7.heading_check_enabled, "gate_exit_yaw_deg 是 null 時不啟用")
m7.update_heading(90.0)
check([r for _, r in replay(m7, simulate_crossing("entering"))] == ["entering"],
      "未啟用時不會拿沒校正的角度擋掉真的穿越")

m8 = new_monitor(expected_exit_yaw_deg=0.0, **HEAD_OPTS)
check([r for _, r in replay(m8, simulate_crossing("entering"))] == ["entering"],
      "啟用了但還沒收到任何航向讀數時放行")

print("\n[6] 不是門口那兩顆的 Beacon 要被忽略")
m9 = new_monitor(min_confirm_samples=1)
check(m9.process_observation("AISLE-03", -40, 1.0) is None, "走道地標點不影響門口判定")

print("\n[7] 時間窗：歷史只留窗口內的樣本，不會無限成長")
m10 = new_monitor(smoothing_sec=0.4)
for i in range(1000):
    m10.process_observation(INSIDE, -60, i * 0.02)
    m10.process_observation(OUTSIDE, -70, i * 0.02)
sizes = [len(v) for v in m10._history.values()]
check(all(n <= 25 for n in sizes), "0.4 秒窗口 @50Hz 大約留 20 筆左右", sizes)

# 兩顆速率差 3 倍時，時間窗要讓兩邊涵蓋同一段時間（這是換成時間窗的主因）
m10b = new_monitor(smoothing_sec=0.4)
t = 0.0
for i in range(120):
    t += 0.02
    m10b.process_observation(OUTSIDE, -70, t)          # 快的：每 20 ms
    if i % 3 == 0:
        m10b.process_observation(INSIDE, -60, t)        # 慢的：每 60 ms
spans = {}
for bid, hist in m10b._history.items():
    spans[bid] = hist[-1][0] - hist[0][0] if len(hist) > 1 else 0.0
check(abs(spans[INSIDE] - spans[OUTSIDE]) < 0.1,
      "速率差 3 倍時，兩邊的歷史仍涵蓋差不多的時間長度（時間窗的主要目的）",
      {k: round(v, 2) for k, v in spans.items()})

print("\n[8] RSSI 校正偏移量（用 2026-09-15 的實測數字）")
# 實測：車子離 OUTSIDE 1.7m、離 INSIDE 3.6m，但 INSIDE 反而強 6 dB
# （兩顆不同廠牌，發射功率差很多）。這一段驗證：不校正會判反，校正後才會對。
REAL_INSIDE, REAL_OUTSIDE = -64.0, -70.0   # 兩者在該位置的實測中位數

m11 = new_monitor(min_confirm_samples=1, hysteresis_dbm=4.0, min_crossing_rssi_dbm=-75.0)
for i in range(20):
    m11.process_observation(INSIDE, REAL_INSIDE, i * 0.02)
    m11.process_observation(OUTSIDE, REAL_OUTSIDE, i * 0.02)
check(m11._detector._dominant == "a",
      "未校正時：明明離外側近，卻判定成靠內側（這就是要修的 bug）",
      m11._detector._dominant)

# 校正後：在這個位置上兩顆應該打平，誰都不佔優勢
offset = REAL_OUTSIDE - REAL_INSIDE   # = -6.0，補在內側
m12 = new_monitor(min_confirm_samples=1, hysteresis_dbm=4.0, min_crossing_rssi_dbm=-75.0,
                  rssi_offsets={INSIDE: offset, OUTSIDE: 0.0})
for i in range(20):
    m12.process_observation(INSIDE, REAL_INSIDE, i * 0.02)
    m12.process_observation(OUTSIDE, REAL_OUTSIDE, i * 0.02)
check(m12._detector._dominant is None, "校正後：在該位置上誰都不佔優勢（正確）",
      m12._detector._dominant)

# 校正後往外側再靠近 6 dB -> 外側應該勝出
m13 = new_monitor(min_confirm_samples=2, hysteresis_dbm=4.0, min_crossing_rssi_dbm=-75.0,
                  rssi_offsets={INSIDE: offset, OUTSIDE: 0.0})
for i in range(20):
    m13.process_observation(INSIDE, REAL_INSIDE, i * 0.02)
    m13.process_observation(OUTSIDE, REAL_OUTSIDE, i * 0.02)
for i in range(20, 60):
    m13.process_observation(INSIDE, REAL_INSIDE - 3, i * 0.02)
    m13.process_observation(OUTSIDE, REAL_OUTSIDE + 6, i * 0.02)
check(m13._detector._dominant == "b", "校正後往外側靠近，外側正確勝出", m13._detector._dominant)

print("\n[9] 某一顆收不到之後就不判定（時間窗會把過期樣本丟掉）")
m14 = new_monitor(min_confirm_samples=1, min_samples=2, smoothing_sec=0.4)
for i in range(10):
    m14.process_observation(INSIDE, -55, i * 0.02)
    m14.process_observation(OUTSIDE, -80, i * 0.02)
# 外側從此消失，只剩內側繼續回報
stale = [m14.process_observation(INSIDE, -55, 10.0 + i * 0.02) for i in range(10)]
check(all(r is None for r in stale), "另一顆消失後不再判定", stale)
check(m14.skipped_too_few_count > 0, "有記錄因為樣本不足而跳過的次數",
      m14.skipped_too_few_count)

print("\n[10] 濾波估計量（用 2026-09-15 的實測序列）")
# 實測的單邊雜訊：訊號很少變強，但常常因為多路徑破壞性干涉突然掉 6 dB。
REAL_OUT = [-70,-75,-71,-70,-75,-71,-70,-75,-70,-70,-70,-75,-70,-70,-69]
check(smooth_rssi(REAL_OUT, 5, "p75") == -70.0, "p75 落在穩定的上包絡線",
      smooth_rssi(REAL_OUT, 5, "p75"))
check(smooth_rssi(REAL_OUT, 5, "mean") < smooth_rssi(REAL_OUT, 5, "p75"),
      "mean 會被下方的 deep fade 拉低")
check(smooth_rssi(REAL_OUT, 5, "max") >= smooth_rssi(REAL_OUT, 5, "p90") >=
      smooth_rssi(REAL_OUT, 5, "p75") >= smooth_rssi(REAL_OUT, 5, "median"),
      "估計量的大小順序符合預期（max ≥ p90 ≥ p75 ≥ median）")

# 靜止時的殘餘抖動：p75 要明顯優於 median
def residual(series, method, n=5):
    import statistics
    vals = [smooth_rssi(series[max(0, i - n + 1):i + 1], n, method) for i in range(n - 1, len(series))]
    return statistics.pstdev(vals)
REAL_IN = [-63,-64,-64,-64,-69,-70,-69,-64,-63,-64,-64,-70,-70,-64]
r_med, r_p75 = residual(REAL_IN, "median"), residual(REAL_IN, "p75")
check(r_p75 < r_med, f"p75 的殘餘抖動小於 median（{r_p75:.2f} < {r_med:.2f}）")
check(r_p75 < 1.0, "p75 殘餘抖動壓在 1 dB 以內", round(r_p75, 2))

for bad in ("nope", ""):
    try:
        smooth_rssi([-70], 1, bad)
        check(False, f"不認得的估計量 {bad!r} 應該要拋錯")
    except ValueError:
        check(True, f"不認得的估計量 {bad!r} 會拋錯")

print("\n[11] 樣本太少時不判定")
m17 = new_monitor(min_samples=3, smoothing_sec=0.4)
few = [m17.process_observation(INSIDE, -55, 0.02 * i) for i in range(2)]
few += [m17.process_observation(OUTSIDE, -80, 0.02 * i) for i in range(2)]
check(all(r is None for r in few), "各只有 2 筆、門檻 3 筆時不判定", few)
check(m17.skipped_too_few_count > 0, "有記錄因為樣本不足而跳過的次數",
      m17.skipped_too_few_count)

print("\n[12] describe() 會警告還沒校正")
m15 = new_monitor()
check("還沒做過門口校正" in m15.describe(), "偏移量全是 0 時 describe() 要警告")
m16 = new_monitor(rssi_offsets={INSIDE: -6.0, OUTSIDE: 0.0})
check("還沒做過門口校正" not in m16.describe(), "有校正值時不警告")

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("全部通過")
