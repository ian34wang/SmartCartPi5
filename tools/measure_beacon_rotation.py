"""
tools/measure_beacon_rotation.py

門口 Beacon 組（兩個鍋子背對背黏成一組）的「轉角度」量測：找出整組要固定在哪個
角度安裝，兩顆的極化方向才會跟 Pi 對齊。

為什麼要量：
    天線是線性極化的。發射端與接收端的極化夾角 ψ 造成的損失是
    -20·log10|cos ψ| dB（45° 掉 3 dB、75° 掉 11.7 dB）。2026-10-28 把兩個鍋子
    黏成一組之後，兩顆的正面同時掉了 11~12 dB、背面只掉 2~3 dB——直達路徑對
    極化很敏感、經過反射的背面訊號不敏感，這個模式最像極化沒對齊。
    肉眼看 drivers.ble_beacon_scanner 的原始讀數分不出幾 dB 的差異（原始值本身
    就會跳 10 dB），所以用這支照系統同一套濾波算出來比。

怎麼轉（重要）：
    - 旋轉軸是「鍋子的中心軸」：水平、穿過兩個鍋底中心、指向 Pi。像轉方向盤
      一樣轉，開口始終正對 Pi。不要繞垂直桌面的軸轉——那樣開口會轉走，量到的
      就變成正面/側面/背面的差異，跟極化混在一起。
    - 把手當時鐘指針：把手朝正下方 = 0°，往同一個方向轉 45°、90°（水平）、
      135°、180°（朝正上方）。每個角度用書或膠帶卡住，人離開再按 Enter。
    - 第 1 輪：門內那顆（GATE-INSIDE）的開口朝 Pi，轉一輪。
      第 2 輪：整組繞「垂直桌面」的軸轉 180°，換門外那顆（GATE-OUTSIDE）開口朝
      Pi，一樣用把手當指針再轉一輪。這兩輪正好對應實際裝在門口時，推車在門內
      與門外看到的情況（整組不動、推車換邊）。
    - Pi 不要動，距離維持一樣；兩條 USB 線都往遠離 Pi 的方向拉。
    - 兩顆 Beacon 都要有電：每個角度兩顆同時記錄，直接算出系統真正用的差分值
      （內側 − 外側）。

結果怎麼看：
    每個角度算兩個數字（都是 GateMonitor 同一套濾波後、還沒加偏移量的值）：
      變化幅度 = 第 1 輪差分平均 − 第 2 輪差分平均
                 推車從門內走到門外時，系統看到的差分值會變多少。越大越好。
      最差間隔 = 第 1 輪差分最小值 − 第 2 輪差分最大值
                 最倒楣的瞬間兩邊還隔多遠；正數代表兩邊從來沒重疊。
    建議的安裝角度取「最差間隔」最大的那個。0° 跟 180° 對線性極化來說是同一個
    方向，兩者差太多代表擺放有其他變化（線材、位置），會另外提醒。

用法：
    python3 -m tools.measure_beacon_rotation --distance-cm 100
    python3 -m tools.measure_beacon_rotation --distance-cm 100 --seconds 20
    python3 -m tools.measure_beacon_rotation --distance-cm 100 --angles 0,30,60,90,120,150,180

    每個角度開始前會停下來等 Enter；輸入 s 跳過該角度。

結果存在 data/beacon_rotation/<時間>/：
    packets.csv    每一筆封包（第幾輪、角度、哪一顆、時間、RSSI）
    summary.json   每個角度兩顆的統計、差分、以及建議角度
"""

from __future__ import annotations

import argparse
import csv
import json
import queue
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.landmark_correction import load_landmark_config  # noqa: E402
from drivers.ble_beacon_scanner import (  # noqa: E402
    BleBeaconScanner,
    load_beacon_identity_map,
    load_beacon_ids,
)
from tools.measure_beacon_shield import (  # noqa: E402
    Packet,
    _drain,
    _fmt,
    _pad,
    analyze,
    filtered_series,
)

_OUT_ROOT = Path(__file__).resolve().parent.parent / "data" / "beacon_rotation"
WAIT_BEACON_SEC = 20.0
SYMMETRY_WARN_DB = 3.0   # 0° 與 180° 的正面強度差超過這個值就提醒


# ----------------------------------------------------------------------
# 純計算（tests/test_measure_beacon_rotation.py 測這一段）
# ----------------------------------------------------------------------
def summarize_step(
    t_start: float,
    t_end: float,
    inside_pk: List[Packet],
    outside_pk: List[Packet],
    window_sec: float,
    min_samples: int,
    method: str,
) -> dict:
    """一個角度的統計：兩顆各自的 analyze()，加上逐格點對齊的差分（內側 − 外側）。"""
    si = filtered_series(t_start, t_end, inside_pk, window_sec, min_samples, method)
    so = filtered_series(t_start, t_end, outside_pk, window_sec, min_samples, method)
    diffs = [a - b for (_, a), (_, b) in zip(si, so) if a is not None and b is not None]
    diff = None
    if diffs:
        diff = {
            "mean": round(statistics.mean(diffs), 2),
            "min": round(min(diffs), 2),
            "max": round(max(diffs), 2),
            "stdev": round(statistics.pstdev(diffs), 2),
            "valid_ratio": round(len(diffs) / len(si), 3) if si else 0.0,
        }
    return {
        "inside": analyze(t_start, t_end, inside_pk, window_sec, min_samples, method),
        "outside": analyze(t_start, t_end, outside_pk, window_sec, min_samples, method),
        "diff": diff,
    }


def rank_angles(sweep_in: Dict[str, dict], sweep_out: Dict[str, dict]) -> List[dict]:
    """兩輪都有量到、而且差分算得出來的角度，依「最差間隔」由大到小排序。

    sweep_in：第 1 輪（門內那顆開口朝 Pi，模擬推車在門內）
    sweep_out：第 2 輪（門外那顆開口朝 Pi，模擬推車在門外）
    """
    rows = []
    for angle, a in sweep_in.items():
        b = sweep_out.get(angle)
        if b is None or a["diff"] is None or b["diff"] is None:
            continue
        rows.append({
            "angle": angle,
            "swing_db": round(a["diff"]["mean"] - b["diff"]["mean"], 2),
            "margin_db": round(a["diff"]["min"] - b["diff"]["max"], 2),
            "inside_front": (a["inside"]["filtered"] or {}).get("mean"),
            "outside_front": (b["outside"]["filtered"] or {}).get("mean"),
        })
    rows.sort(key=lambda r: r["margin_db"], reverse=True)
    return rows


def symmetry_warnings(sweep: Dict[str, dict], front: str) -> List[str]:
    """0° 和 180° 對線性極化是同一個方向，正面強度應該差不多。"""
    a, b = sweep.get("0"), sweep.get("180")
    if not a or not b:
        return []
    fa = (a[front]["filtered"] or {}).get("mean")
    fb = (b[front]["filtered"] or {}).get("mean")
    if fa is None or fb is None or abs(fa - fb) <= SYMMETRY_WARN_DB:
        return []
    return [f"0° 與 180° 的正面差了 {abs(fa - fb):.1f} dB（理論上應該差不多）——"
            "轉的時候位置、距離或線材可能跟著變了，這一輪的結果要打折扣。"]


# ----------------------------------------------------------------------
# 量測（碰硬體）
# ----------------------------------------------------------------------
def collect_multi(scanner: BleBeaconScanner, beacon_ids: Sequence[str],
                  seconds: float) -> Tuple[float, float, Dict[str, List[Packet]]]:
    _drain(scanner.out_queue)
    wanted = {b.upper(): b for b in beacon_ids}
    t_start = time.time()
    t_end = t_start + seconds
    packets: Dict[str, List[Packet]] = {b: [] for b in beacon_ids}
    last_print = t_start
    while True:
        now = time.time()
        if now >= t_end:
            break
        try:
            obs = scanner.out_queue.get(timeout=0.1)
        except queue.Empty:
            obs = None
        if obs is not None and obs.timestamp >= t_start:
            key = wanted.get(obs.beacon_id.upper())
            if key is not None:
                packets[key].append((obs.timestamp, obs.rssi))
        if now - last_print >= 1.0:
            last_print = now
            parts = []
            for b in beacon_ids:
                pk = packets[b]
                last = f"{pk[-1][1]:>4}" if pk else "  --"
                parts.append(f"{b} {len(pk):>3} 包 最新 {last}")
            print(f"\r  收集中 剩 {t_end - now:4.1f}s　" + "　".join(parts) + "   ", end="", flush=True)
    print()
    return t_start, t_end, packets


def print_sweep(title: str, sweep: Dict[str, dict], front: str, back: str,
                inside_id: str, outside_id: str) -> None:
    names = {"inside": inside_id, "outside": outside_id}
    print(f"\n{title}")
    header = (_pad("角度", 8) + _pad(f"{names[front]}（正面）", 22) + _pad("包/秒", 8)
              + _pad(f"{names[back]}（背面）", 22) + _pad("包/秒", 8) + "差分 內−外（平均 / 範圍）")
    print(header)
    print("-" * 96)
    for angle, r in sweep.items():
        f, b, d = r[front], r[back], r["diff"]
        drange = "--" if d is None else f"{d['mean']:+6.1f}  ({d['min']:+.1f} ~ {d['max']:+.1f})"
        print(_pad(f"{angle}°", 8)
              + _pad(_fmt((f["filtered"] or {}).get("mean")), 22) + _pad(_fmt(f["rate_pps"], 1), 8)
              + _pad(_fmt((b["filtered"] or {}).get("mean")), 22) + _pad(_fmt(b["rate_pps"], 1), 8)
              + drange)


def _main() -> int:
    parser = argparse.ArgumentParser(description="門口 Beacon 組轉角度量測：找出極化對齊的安裝角度")
    parser.add_argument("--distance-cm", type=float, required=True, help="Beacon 組到 Pi 的距離（公分），只記錄用")
    parser.add_argument("--seconds", type=float, default=15.0, help="每個角度收集幾秒，預設 15")
    parser.add_argument("--angles", default="0,45,90,135,180", help="要量的角度，逗號分隔，預設 0,45,90,135,180")
    parser.add_argument("--note", default="", help="備註，只記錄用")
    parser.add_argument("--adapter", default=None, help="藍牙介面名稱，預設用系統預設")
    args = parser.parse_args()

    try:
        angles = [str(int(a.strip())) for a in args.angles.split(",") if a.strip()]
    except ValueError:
        print(f"[錯誤] --angles 要是逗號分隔的整數，收到：{args.angles!r}")
        return 1

    cfg = load_landmark_config()
    pair = cfg.get("gate_beacon_pair", {})
    inside_id, outside_id = pair.get("inside_id"), pair.get("outside_id")
    if not inside_id or not outside_id:
        print("[錯誤] config.json 的 landmarks.gate_beacon_pair 沒有設定 inside_id / outside_id")
        return 1
    window_sec = float(cfg.get("rssi_smoothing_sec", 1.0))
    min_samples = int(cfg.get("rssi_min_samples", 3))
    method = cfg.get("rssi_estimator", "p75")

    print(f"啟動藍牙掃描，等待 {inside_id} 與 {outside_id} 都出現（最多 {WAIT_BEACON_SEC:.0f} 秒）...", flush=True)
    scanner: Optional[BleBeaconScanner] = None
    try:
        scanner = BleBeaconScanner(
            beacon_ids=load_beacon_ids(),
            address_map=load_beacon_identity_map(),
            out_queue=queue.Queue(),
            adapter=args.adapter,
        )
        scanner.start()
        scanner.wait_for_beacons([inside_id, outside_id], timeout_sec=WAIT_BEACON_SEC)
    except KeyboardInterrupt:
        if scanner is not None:
            scanner.stop()
        print("\n中斷。")
        return 1
    except Exception as exc:  # noqa: BLE001
        if scanner is not None:
            scanner.stop()
        print(f"\n[錯誤] {exc}")
        print("  這支需要門口兩顆 Beacon 同時有電；只量單一顆用 python3 -m tools.measure_beacon_shield")
        return 1
    print(f"兩顆都掃到了。每個角度收集 {args.seconds:.0f} 秒，角度：{', '.join(a + '°' for a in angles)}\n")

    started = datetime.now()
    out_dir = _OUT_ROOT / f"{started:%Y%m%d-%H%M%S}"
    sweeps: Dict[str, Dict[str, dict]] = {"inside_facing": {}, "outside_facing": {}}
    raw: List[Tuple[str, str, float, Dict[str, List[Packet]]]] = []
    plan = [
        ("inside_facing", f"第 1 輪：{inside_id} 開口朝 Pi"),
        ("outside_facing", f"第 2 輪：整組繞垂直桌面的軸轉 180°，換 {outside_id} 開口朝 Pi"),
    ]

    try:
        for sweep_key, title in plan:
            print(f"── {title} ──")
            for angle in angles:
                ans = input(f"  把手轉到 {angle}°（0° = 把手朝正下方），卡好、人離開後按 Enter（s = 跳過）：")
                if ans.strip().lower() == "s":
                    continue
                t_start, t_end, pk = collect_multi(scanner, [inside_id, outside_id], args.seconds)
                r = summarize_step(t_start, t_end, pk[inside_id], pk[outside_id],
                                   window_sec, min_samples, method)
                sweeps[sweep_key][angle] = r
                raw.append((sweep_key, angle, t_start, pk))
                front = "inside" if sweep_key == "inside_facing" else "outside"
                fm = (r[front]["filtered"] or {}).get("mean")
                dm = (r["diff"] or {}).get("mean")
                print(f"  → 正面濾波後 {_fmt(fm)}　差分（內−外）{_fmt(dm)}\n")
    except KeyboardInterrupt:
        print("\n中斷，已經量完的角度還是會存檔。")
    finally:
        scanner.stop()

    if not raw:
        print("一個角度都沒量，不存檔。")
        return 1

    print("=" * 96)
    print(f"距離 {args.distance_cm:.0f} cm　窗口 {window_sec:g} 秒 / {method} / 至少 {min_samples} 包　{args.note}")
    print("（數值都是濾波後、還沒加 rssi_offset_db 的 dBm；偏移量是常數，不影響下面的比較）")
    print("=" * 96)
    if sweeps["inside_facing"]:
        print_sweep(f"第 1 輪：{inside_id} 開口朝 Pi（模擬推車在門內，差分應該偏高）",
                    sweeps["inside_facing"], "inside", "outside", inside_id, outside_id)
    if sweeps["outside_facing"]:
        print_sweep(f"第 2 輪：{outside_id} 開口朝 Pi（模擬推車在門外，差分應該偏低）",
                    sweeps["outside_facing"], "outside", "inside", inside_id, outside_id)

    ranking = rank_angles(sweeps["inside_facing"], sweeps["outside_facing"])
    warnings = (symmetry_warnings(sweeps["inside_facing"], "inside")
                + symmetry_warnings(sweeps["outside_facing"], "outside"))

    if ranking:
        print("\n各角度比較（依最差間隔排序）")
        print(_pad("角度", 8) + _pad("變化幅度", 12) + _pad("最差間隔", 12)
              + _pad(f"{inside_id} 正面", 18) + f"{outside_id} 正面")
        print("-" * 96)
        for row in ranking:
            print(_pad(f"{row['angle']}°", 8) + _pad(_fmt(row["swing_db"], 1, " dB"), 12)
                  + _pad(_fmt(row["margin_db"], 1, " dB"), 12)
                  + _pad(_fmt(row["inside_front"]), 18) + _fmt(row["outside_front"]))
        best = ranking[0]
        print(f"\n建議安裝角度：{best['angle']}°（最差間隔 {best['margin_db']:+.1f} dB、"
              f"變化幅度 {best['swing_db']:.1f} dB）")
        if best["margin_db"] <= 0:
            print("  [注意] 最好的角度最差間隔也 ≤ 0：門內門外的差分有重疊，光靠調角度不夠。")
    else:
        print("\n兩輪沒有共同量到的角度（或差分算不出來），沒辦法比較。")

    for w in warnings:
        print(f"  [注意] {w}")

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "packets.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sweep", "angle_deg", "beacon_id", "t_rel_sec", "epoch", "rssi"])
        for sweep_key, angle, t_start, pk in raw:
            for beacon_id, packets in pk.items():
                for ts, rssi in packets:
                    w.writerow([sweep_key, angle, beacon_id, f"{ts - t_start:.4f}", f"{ts:.4f}", rssi])
    summary = {
        "meta": {
            "inside_id": inside_id,
            "outside_id": outside_id,
            "distance_cm": args.distance_cm,
            "seconds_per_angle": args.seconds,
            "angles": angles,
            "note": args.note,
            "started_at": started.isoformat(timespec="seconds"),
            "rssi_smoothing_sec": window_sec,
            "rssi_min_samples": min_samples,
            "rssi_estimator": method,
        },
        "sweeps": sweeps,
        "ranking": ranking,
        "warnings": warnings,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
    print(f"\n已存檔：{out_dir}/packets.csv、summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
