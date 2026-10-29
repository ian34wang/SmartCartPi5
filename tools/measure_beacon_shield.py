"""
tools/measure_beacon_shield.py

單顆 Beacon 的遮罩（指向性）量測：同一個距離下，量「開口側」與「遮擋側」（以及
選填的「無遮罩基準」）各自的 RSSI 與封包量，存成原始資料 + 摘要，拿來決定門口
判定的演算法與校正值。

為什麼不能只看 RSSI 平均：
    2026-10 用鐵盆實測：遮起來之後 RSSI 只掉約 10 dB，但更新明顯變慢。掉進深度
    衰減的封包根本收不到，不會出現在任何平均裡——RSSI 平均只描述「活下來的封
    包」，會低估真正的遮蔽效果。所以這支同時記錄：
      - 封包量與到達率：收不到本身就是訊號。
      - 封包間隔：最長斷訊多久、超過 1 秒的斷訊幾次。
      - 可判定時間比例：照 config.json 的 rssi_smoothing_sec / rssi_min_samples，
        每 0.1 秒檢查一次「窗口內的樣本夠不夠 GateMonitor 做判定」。刻意用固定
        的時間格點、不用封包去算——用封包算一樣有倖存者偏差，沒封包的時段根本
        不會被檢查到。
      - 濾波後的值：跟 GateMonitor 同一個估計量（rssi_estimator），也就是系統
        真正會看到的數字。
      - 連續相同 RSSI 的比例：自我檢查用。Linux 的 BlueZ 有可能把連續相同的
        RSSI 合併成一次回報，那樣封包數會被低估。整數 RSSI 加上幾 dB 的雜訊，
        正常情況下連續兩筆相同的比例應該有一成以上；如果是 0%，代表封包數只能
        當下限看。

量測方式（數據能不能用就看這個）：
    - Pi 固定不動。Beacon 跟 Pi 保持同一個距離、同一個高度。
    - 換「開口側／遮擋側」時，把 Beacon 連同遮罩「原地轉 180 度」，不要移動 Pi
      ——移動 Pi 會連周圍的反射環境一起換掉，量到的差異就不只是遮罩造成的。
    - 量測期間人不要站在 Beacon 跟 Pi 之間（人體對 2.4 GHz 衰減很大）。
    - Pi 5 的 Wi-Fi 跟藍牙共用同一顆射頻晶片，Wi-Fi 很忙的時候藍牙掃描會漏封
      包。各狀態之間盡量維持相同的網路使用狀況，不要一個狀態剛好在下載東西。
    - 只需要這一顆 Beacon 有電，另一顆不用。

用法：
    python3 -m tools.measure_beacon_shield --beacon GATE-OUTSIDE --distance-cm 100
    python3 -m tools.measure_beacon_shield --beacon GATE-INSIDE --distance-cm 100 --seconds 60
    python3 -m tools.measure_beacon_shield --beacon GATE-OUTSIDE --distance-cm 100 --note "鐵盆"

    每個狀態開始前會停下來等你按 Enter；輸入 s 可以跳過該狀態（例如不量基準）。

結果存在 data/beacon_shield/<時間>_<beacon>/：
    packets.csv    每一筆收到的封包（狀態、相對時間、RSSI）
    summary.json   各狀態的統計，以及兩兩之間的差值
"""

from __future__ import annotations

import argparse
import csv
import json
import queue
import statistics
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.landmark_correction import load_landmark_config, smooth_rssi  # noqa: E402
from drivers.ble_beacon_scanner import (  # noqa: E402
    BleBeaconScanner,
    load_beacon_identity_map,
    load_beacon_ids,
)

_OUT_ROOT = Path(__file__).resolve().parent.parent / "data" / "beacon_shield"

# (代號, 顯示名稱, 要怎麼擺)
CONDITIONS: List[Tuple[str, str, str]] = [
    ("baseline", "無遮罩", "把遮罩拿掉，Beacon 裸放在原位"),
    ("open", "開口側", "裝上遮罩，開口朝向 Pi"),
    ("shield", "遮擋側", "Beacon 連同遮罩原地轉 180 度，遮擋面朝向 Pi"),
]
# 要比較的組合：(A, B) 代表報告 A − B
COMPARISONS = [("open", "shield"), ("baseline", "open"), ("baseline", "shield")]

TICK_SEC = 0.1            # 可判定時間比例的檢查間隔
DROPOUT_SEC = 1.0         # 封包間隔超過這個值算一次「斷訊」
WAIT_BEACON_SEC = 20.0


Packet = Tuple[float, int]  # (epoch 秒, rssi)


# ----------------------------------------------------------------------
# 純計算（不碰硬體，tests/test_measure_beacon_shield.py 測這一段）
# ----------------------------------------------------------------------
def percentile(sorted_vals: Sequence[float], q: float) -> float:
    """線性內插的百分位數，跟 core.landmark_correction.smooth_rssi 的 p75/p90 同一種算法。"""
    n = len(sorted_vals)
    k = (n - 1) * q
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def filtered_series(
    t_start: float,
    t_end: float,
    packets: List[Packet],
    window_sec: float,
    min_samples: int,
    method: str,
) -> List[Tuple[float, Optional[float]]]:
    """固定時間格點上的濾波值，跟 GateMonitor 同一種算法。

    從 t_start + window_sec 開始，每 TICK_SEC 一個格點；每個格點往回看 window_sec，
    窗口內至少 min_samples 筆就用 smooth_rssi(method) 算出代表值，不夠就給 None
    （代表 GateMonitor 在這個時刻不會做判定）。同一段量測的不同 Beacon 用同一組
    t_start/t_end 呼叫，格點就會一一對齊，可以直接逐點相減算差分。
    """
    packets = sorted(p for p in packets if t_start <= p[0] < t_end)
    times = [p[0] for p in packets]
    rssi = [p[1] for p in packets]
    n = len(packets)
    out: List[Tuple[float, Optional[float]]] = []
    lo = 0
    hi = 0
    t = t_start + window_sec
    while t <= t_end + 1e-9:
        while hi < n and times[hi] <= t:
            hi += 1
        while lo < hi and times[lo] <= t - window_sec:
            lo += 1
        window = rssi[lo:hi]
        value = None
        if len(window) >= min_samples:
            value = smooth_rssi([float(v) for v in window], len(window), method)
        out.append((t, value))
        t += TICK_SEC
    return out


def analyze(
    t_start: float,
    t_end: float,
    packets: List[Packet],
    window_sec: float,
    min_samples: int,
    method: str,
) -> dict:
    """一個狀態的統計。packets 是 [(epoch, rssi)]，只算 t_start <= t < t_end 的。"""
    packets = sorted(p for p in packets if t_start <= p[0] < t_end)
    duration = t_end - t_start
    n = len(packets)
    out: dict = {
        "duration_sec": round(duration, 2),
        "packets": n,
        "rate_pps": round(n / duration, 2) if duration > 0 else None,
        "rssi": None,
        "gaps": None,
        "repeat_ratio": None,
        "judgeable_ratio": 0.0,
        "filtered": None,
    }
    if n == 0:
        out["gaps"] = {"median_ms": None, "p95_ms": None, "max_ms": round(duration * 1000),
                       "dropouts": 1, "dropout_time_ratio": 1.0}
        return out

    rssi = [p[1] for p in packets]
    s = sorted(rssi)
    out["rssi"] = {
        "mean": round(statistics.mean(rssi), 2),
        "median": round(float(statistics.median(rssi)), 2),
        "p75": round(percentile(s, 0.75), 2),
        "p90": round(percentile(s, 0.90), 2),
        "max": s[-1],
        "min": s[0],
        "stdev": round(statistics.pstdev(rssi), 2),
    }

    times = [p[0] for p in packets]
    inner = [b - a for a, b in zip(times, times[1:])]
    # 斷訊要連頭尾一起算：開始量之後很久才收到第一包，也是斷訊
    edges = [t_start] + times + [t_end]
    all_gaps = [b - a for a, b in zip(edges, edges[1:])]
    long_gaps = [g for g in all_gaps if g > DROPOUT_SEC]
    inner_sorted = sorted(inner)
    out["gaps"] = {
        "median_ms": round(statistics.median(inner) * 1000, 1) if inner else None,
        "p95_ms": round(percentile(inner_sorted, 0.95) * 1000, 1) if inner else None,
        "max_ms": round(max(all_gaps) * 1000, 1),
        "dropouts": len(long_gaps),
        "dropout_time_ratio": round(sum(long_gaps) / duration, 3) if duration > 0 else None,
    }
    if n > 1:
        out["repeat_ratio"] = round(sum(1 for a, b in zip(rssi, rssi[1:]) if a == b) / (n - 1), 3)

    series = filtered_series(t_start, t_end, packets, window_sec, min_samples, method)
    filtered = [v for _, v in series if v is not None]
    out["judgeable_ratio"] = round(len(filtered) / len(series), 3) if series else 0.0
    if filtered:
        out["filtered"] = {
            "method": method,
            "mean": round(statistics.mean(filtered), 2),
            "min": round(min(filtered), 2),
            "max": round(max(filtered), 2),
            "stdev": round(statistics.pstdev(filtered), 2),
        }
    return out


def compare(a: dict, b: dict) -> dict:
    """A − B。任一邊沒有資料的欄位給 None。"""
    def diff(x, y):
        return None if x is None or y is None else round(float(x) - float(y), 2)

    ra, rb = a.get("rssi") or {}, b.get("rssi") or {}
    fa, fb = a.get("filtered") or {}, b.get("filtered") or {}
    rate_ratio = None
    if a.get("rate_pps") and b.get("rate_pps"):
        rate_ratio = round(a["rate_pps"] / b["rate_pps"], 2)
    return {
        "rssi_mean_db": diff(ra.get("mean"), rb.get("mean")),
        "rssi_median_db": diff(ra.get("median"), rb.get("median")),
        "rssi_p75_db": diff(ra.get("p75"), rb.get("p75")),
        "rssi_max_db": diff(ra.get("max"), rb.get("max")),
        "filtered_mean_db": diff(fa.get("mean"), fb.get("mean")),
        "rate_ratio": rate_ratio,
        "judgeable_ratio_diff": diff(a.get("judgeable_ratio"), b.get("judgeable_ratio")),
    }


# ----------------------------------------------------------------------
# 量測（碰硬體）
# ----------------------------------------------------------------------
def _drain(q: "queue.Queue") -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def collect(scanner: BleBeaconScanner, beacon_id: str, seconds: float) -> Tuple[float, float, List[Packet]]:
    """收集 seconds 秒；等待期間累積在 queue 裡的舊封包先丟掉，不算進這個狀態。"""
    _drain(scanner.out_queue)
    t_start = time.time()
    t_end = t_start + seconds
    packets: List[Packet] = []
    last_print = t_start  # 第一秒過後才開始印，避免一開始除以接近 0 的時間
    while True:
        now = time.time()
        if now >= t_end:
            break
        try:
            obs = scanner.out_queue.get(timeout=0.1)
        except queue.Empty:
            obs = None
        if obs is not None and obs.beacon_id.upper() == beacon_id.upper() and obs.timestamp >= t_start:
            packets.append((obs.timestamp, obs.rssi))
        if now - last_print >= 1.0:
            last_print = now
            elapsed = max(now - t_start, 1e-6)
            last = f"{packets[-1][1]:>4} dBm" if packets else "   --   "
            print(f"\r  收集中 剩 {t_end - now:4.1f}s　封包 {len(packets):>4}"
                  f"（{len(packets) / elapsed:4.1f} 包/秒）　最新 {last}   ", end="", flush=True)
    print()
    return t_start, t_end, packets


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, w: int) -> str:
    return s + " " * max(w - _width(s), 0)


def _fmt(v, nd: int = 1, suffix: str = "") -> str:
    if v is None:
        return "--"
    if isinstance(v, float):
        return f"{v:.{nd}f}{suffix}"
    return f"{v}{suffix}"


def print_table(results: Dict[str, dict]) -> None:
    keys = [k for k, _, _ in CONDITIONS if k in results]
    names = {k: name for k, name, _ in CONDITIONS}
    col = 10

    def row(label: str, getter, nd: int = 1, suffix: str = "") -> None:
        cells = "".join(f"{_fmt(getter(results[k]), nd, suffix):>{col}}" for k in keys)
        print(_pad(label, 24) + cells)

    print(_pad("", 24) + "".join(" " * (col - _width(names[k])) + names[k] for k in keys))
    print("-" * (24 + col * len(keys)))
    row("封包數", lambda r: r["packets"])
    row("到達率（包/秒）", lambda r: r["rate_pps"], 2)
    row("RSSI 平均", lambda r: (r["rssi"] or {}).get("mean"))
    row("RSSI 中位數", lambda r: (r["rssi"] or {}).get("median"))
    row("RSSI p75", lambda r: (r["rssi"] or {}).get("p75"))
    row("RSSI 最大", lambda r: (r["rssi"] or {}).get("max"))
    row("RSSI 最小", lambda r: (r["rssi"] or {}).get("min"))
    row("RSSI 標準差", lambda r: (r["rssi"] or {}).get("stdev"))
    row("封包間隔中位（ms）", lambda r: r["gaps"]["median_ms"], 0)
    row("封包間隔 p95（ms）", lambda r: r["gaps"]["p95_ms"], 0)
    row("最長斷訊（ms）", lambda r: r["gaps"]["max_ms"], 0)
    row(f"斷訊 >{DROPOUT_SEC:.0f}s 次數", lambda r: r["gaps"]["dropouts"])
    row("可判定時間比例", lambda r: r["judgeable_ratio"] * 100, 1, "%")
    row("濾波後平均", lambda r: (r["filtered"] or {}).get("mean"))
    row("連續相同 RSSI 比例", lambda r: None if r["repeat_ratio"] is None else r["repeat_ratio"] * 100, 1, "%")


def print_comparisons(comparisons: Dict[str, dict]) -> None:
    if not comparisons:
        return
    print()
    for label, c in comparisons.items():
        print(f"  {label}：平均 {_fmt(c['rssi_mean_db'], 1, ' dB')}　中位 {_fmt(c['rssi_median_db'], 1, ' dB')}"
              f"　p75 {_fmt(c['rssi_p75_db'], 1, ' dB')}　最大 {_fmt(c['rssi_max_db'], 1, ' dB')}"
              f"　濾波後 {_fmt(c['filtered_mean_db'], 1, ' dB')}　到達率 ×{_fmt(c['rate_ratio'], 2)}")


def save(out_dir: Path, meta: dict, results: Dict[str, dict], comparisons: Dict[str, dict],
         raw: Dict[str, Tuple[float, List[Packet]]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "packets.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["condition", "t_rel_sec", "epoch", "rssi"])
        for cond, (t_start, packets) in raw.items():
            for ts, r in packets:
                w.writerow([cond, f"{ts - t_start:.4f}", f"{ts:.4f}", r])
    summary = {"meta": meta, "conditions": results, "comparisons": comparisons}
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")


def _main() -> int:
    parser = argparse.ArgumentParser(description="單顆 Beacon 遮罩量測：開口側 vs 遮擋側的 RSSI 與封包量")
    parser.add_argument("--beacon", required=True, help="要量的 beacon_id（config.json 的 landmarks.points）")
    parser.add_argument("--distance-cm", type=float, required=True, help="Beacon 到 Pi 的距離（公分），只記錄用")
    parser.add_argument("--seconds", type=float, default=30.0, help="每個狀態收集幾秒，預設 30")
    parser.add_argument("--note", default="", help="備註（遮罩材質、尺寸等），只記錄用")
    parser.add_argument("--adapter", default=None, help="藍牙介面名稱，預設用系統預設")
    args = parser.parse_args()

    ids = load_beacon_ids()
    beacon = next((b for b in ids if b.upper() == args.beacon.upper()), None)
    if beacon is None:
        print(f"[錯誤] config.json 的 landmarks.points 裡沒有 {args.beacon!r}，現有的是：{ids}")
        return 1

    cfg = load_landmark_config()
    window_sec = float(cfg.get("rssi_smoothing_sec", 0.4))
    min_samples = int(cfg.get("rssi_min_samples", 3))
    method = cfg.get("rssi_estimator", "p75")

    print(f"啟動藍牙掃描，等待 {beacon} 出現（最多 {WAIT_BEACON_SEC:.0f} 秒）...", flush=True)
    scanner: Optional[BleBeaconScanner] = None
    try:
        scanner = BleBeaconScanner(
            beacon_ids=ids,
            address_map=load_beacon_identity_map(),
            out_queue=queue.Queue(),
            adapter=args.adapter,
        )
        scanner.start()
        scanner.wait_for_beacons([beacon], timeout_sec=WAIT_BEACON_SEC)
    except KeyboardInterrupt:
        if scanner is not None:
            scanner.stop()
        print("\n中斷。")
        return 1
    except Exception as exc:  # noqa: BLE001
        if scanner is not None:
            scanner.stop()
        print(f"\n[錯誤] {exc}")
        print("  確認 Beacon 有電；藍牙有開（sudo rfkill unblock bluetooth）；"
              "位址/名稱對得上（python3 -m drivers.ble_beacon_scanner --list）")
        return 1
    print(f"掃到 {beacon} 了。每個狀態收集 {args.seconds:.0f} 秒。\n")

    started = datetime.now()
    out_dir = _OUT_ROOT / f"{started:%Y%m%d-%H%M%S}_{beacon}"
    meta = {
        "beacon_id": beacon,
        "address": next((a for a, b in load_beacon_identity_map().items() if b == beacon), ""),
        "distance_cm": args.distance_cm,
        "seconds_per_condition": args.seconds,
        "note": args.note,
        "started_at": started.isoformat(timespec="seconds"),
        "rssi_smoothing_sec": window_sec,
        "rssi_min_samples": min_samples,
        "rssi_estimator": method,
        "tick_sec": TICK_SEC,
        "dropout_sec": DROPOUT_SEC,
    }
    results: Dict[str, dict] = {}
    raw: Dict[str, Tuple[float, List[Packet]]] = {}

    try:
        for key, name, how in CONDITIONS:
            ans = input(f"[{name}] {how}。擺好、人離開之後按 Enter（s = 跳過）：").strip().lower()
            if ans == "s":
                continue
            t_start, t_end, packets = collect(scanner, beacon, args.seconds)
            results[key] = analyze(t_start, t_end, packets, window_sec, min_samples, method)
            raw[key] = (t_start, packets)
            r = results[key]
            mean = (r["rssi"] or {}).get("mean")
            print(f"  → {r['packets']} 包（{r['rate_pps']} 包/秒）　RSSI 平均 {_fmt(mean)}　"
                  f"可判定時間 {r['judgeable_ratio'] * 100:.0f}%\n")
    except KeyboardInterrupt:
        print("\n中斷，已經量完的狀態還是會存檔。")
    finally:
        scanner.stop()

    if not results:
        print("一個狀態都沒量，不存檔。")
        return 1

    names = {k: name for k, name, _ in CONDITIONS}
    comparisons = {
        f"{names[a]} − {names[b]}": compare(results[a], results[b])
        for a, b in COMPARISONS if a in results and b in results
    }

    print("=" * 64)
    print(f"{beacon}　距離 {args.distance_cm:.0f} cm　{args.note}")
    print("=" * 64)
    print_table(results)
    print_comparisons(comparisons)

    repeats = [r["repeat_ratio"] for r in results.values() if r["repeat_ratio"] is not None and r["packets"] >= 100]
    if repeats and max(repeats) == 0.0:
        print("\n  [注意] 連續相同 RSSI 的比例是 0%——BlueZ 可能把相同的讀數合併回報，"
              "封包數只能當下限看（各狀態之間的相對比較仍然有效）。")

    save(out_dir, meta, results, comparisons, raw)
    print(f"\n已存檔：{out_dir}/packets.csv、summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
