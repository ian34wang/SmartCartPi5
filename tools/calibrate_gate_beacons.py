"""
tools/calibrate_gate_beacons.py

管制區門口雙 Beacon 的校正工具。跟 `calibrate_weight.py`/`calibrate_optical_flow.py`
一樣：實際量測 -> 算出參數 -> 問過之後寫回 `config.json`。

為什麼非做不可（2026-09-15 實測結果）：
    門口判定的核心假設是「訊號比較強的那顆 Beacon ＝ 比較近的那顆」。這個假設
    只有在兩顆 Beacon 的實際發射功率與天線增益一樣時才成立。

    實測兩顆不同廠牌的 Beacon：
        車子離 GATE-OUTSIDE 只有 1.7 m、離 GATE-INSIDE 有 3.6 m
        照理說 OUTSIDE 應該比 INSIDE 強約 6.5 dB
        實測卻是 INSIDE 比 OUTSIDE 強 6.0 dB
        => 系統性偏差約 12.5 dB，方向完全相反

    也就是說：不校正的話，車子站在門外側附近，系統仍然會認為它穩穩地在門內側，
    「出場」判定永遠不會觸發。這不是門檻值調一調就能解決的，是兩顆 Beacon 的
    基準本來就不同，必須量出差多少再補回去。

這支工具做的事：
    1. 把車推到「門檻線」——也就是你希望系統判定為『剛好在門口正中間、內外
       未定』的那個位置。收集兩顆 Beacon 各一批 RSSI 樣本。
    2. 算出 `rssi_offset_db`：讓兩顆在這個位置上校正後的訊號強度相等。之後
       只要車子往內側移動，內側就會勝出；往外側移動，外側就會勝出。
    3. 順便從實測雜訊算出建議的 `gate_hysteresis_dbm`（遲滯）與
       `gate_min_crossing_rssi_dbm`（絕對門檻），不用憑感覺猜。
    4. 問過之後寫回 config.json 的 landmarks 區塊。

用法：
    python3 -m tools.calibrate_gate_beacons --monitor       # 即時看原始 vs 濾波後的值
    python3 -m tools.calibrate_gate_beacons --survey        # 勘測：這組佈署夠不夠用
    python3 -m tools.calibrate_gate_beacons                 # 正式校正
    python3 -m tools.calibrate_gate_beacons --dry-run       # 只算給你看，不寫回
    python3 -m tools.calibrate_gate_beacons --seconds 20    # 每個位置收集幾秒（預設 15）
    python3 -m tools.calibrate_gate_beacons --adapter hci0

建議搭配的實體佈署：把兩顆 Beacon 的發射功率都調到最低，讓有效偵測範圍侷限在
門口通道附近。軟體門檻跟實體佈署是互相配合、不是取代關係——BLE RSSI 物理上量
不到「有沒有真的通過那個開口」，範圍縮小本身就是最有效的一道防護。
"""

from __future__ import annotations

import argparse
import json
import queue
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.landmark_correction import load_landmark_config, smooth_rssi  # noqa: E402
from drivers.ble_beacon_scanner import (  # noqa: E402
    BleBeaconScanner,
    load_beacon_identity_map,
    load_beacon_ids,
)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# 遲滯要蓋得過殘餘雜訊，否則靜止不動也會反覆翻轉判定。經驗上取平滑後標準差的
# 兩倍左右；下限 3 dB 是因為實測單筆 RSSI 的峰對峰抖動就有 7 dB。
_HYSTERESIS_SIGMA_MULTIPLIER = 2.0
_HYSTERESIS_MIN_DB = 3.0
# 絕對門檻：比「站在門檻線上」量到的強度再寬鬆這麼多，避免正常通過時反而被擋掉。
_ABS_THRESHOLD_MARGIN_DB = 5.0


def collect(scanner: BleBeaconScanner, beacon_ids: List[str], seconds: float) -> Dict[str, List[float]]:
    """收集指定秒數的樣本，回傳 {beacon_id: [rssi, ...]}。"""
    samples: Dict[str, List[float]] = {b: [] for b in beacon_ids}
    deadline = time.time() + seconds
    last_print = 0.0
    while time.time() < deadline:
        try:
            obs = scanner.out_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        if obs.beacon_id in samples:
            samples[obs.beacon_id].append(float(obs.rssi))
        now = time.time()
        if now - last_print >= 1.0:
            last_print = now
            counts = "　".join(f"{b}={len(v)}" for b, v in samples.items())
            print(f"\r  收集中... 剩 {deadline - now:4.1f}s　{counts}   ", end="", flush=True)
    print()
    return samples


def summarize(name: str, values: List[float]) -> Optional[dict]:
    if len(values) < 5:
        print(f"  [錯誤] {name} 只收到 {len(values)} 筆樣本，太少了（至少要 5 筆）。"
              "確認 Beacon 有電、在偵測範圍內。")
        return None
    stats = {
        "n": len(values),
        "median": statistics.median(values),
        "mean": statistics.mean(values),
        "stdev": statistics.pstdev(values),
        "min": min(values),
        "max": max(values),
    }
    print(f"  {name:<16} n={stats['n']:<4} 中位數={stats['median']:>6.1f} dBm　"
          f"標準差={stats['stdev']:>4.1f}　範圍={stats['min']:.0f}~{stats['max']:.0f}"
          f"（峰對峰 {stats['max'] - stats['min']:.0f} dB）")
    return stats


def residual_after_filter(values: List[float], method: str, n: int = 8) -> float:
    """把實測樣本餵給真正在用的濾波器，量出「車子沒動時輸出還會抖多少」。

    車子是靜止的，所以濾波後輸出的標準差就是純粹的殘餘雜訊。直接量比用公式
    推估可靠——`σ/√N` 只對平均數成立，對中位數要乘 1.253，對 p75 這種百分位
    估計量根本沒有簡單的閉式解。
    """
    if len(values) <= n:
        return statistics.pstdev(values) if len(values) > 1 else 0.0
    out = [smooth_rssi(values[i - n + 1:i + 1], n, method) for i in range(n - 1, len(values))]
    return statistics.pstdev(out) if len(out) > 1 else 0.0


def write_back(offsets: Dict[str, float], hysteresis: float, min_crossing: float) -> None:
    """只改 landmarks 區塊裡的這幾個值，其他欄位與註解原封不動。"""
    raw = _CONFIG_PATH.read_text(encoding="utf-8")
    cfg = json.loads(raw)
    landmarks = cfg.setdefault("landmarks", {})
    for point in landmarks.get("points", []):
        if point["beacon_id"] in offsets:
            point["rssi_offset_db"] = round(offsets[point["beacon_id"]], 1)
    landmarks["gate_hysteresis_dbm"] = round(hysteresis, 1)
    landmarks["gate_min_crossing_rssi_dbm"] = round(min_crossing, 1)
    _CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已寫回 {_CONFIG_PATH}")
    print("（注意：JSON 重新格式化過，原本的排版會變，但欄位與 _comment 註解都還在）")




def run_monitor(scanner, gate_monitor, refresh_sec: float = 0.25) -> None:
    """即時監看：同時顯示「原始讀數」與「濾波後的值」，以及判定結果。

    為什麼需要這個：`python3 -m drivers.ble_beacon_scanner` 印的是原始讀數
    （那支是驅動層，故意不做任何處理，用途是確認 Beacon 認得到、位址對不對），
    肉眼看起來抖動很大——實測靜止時峰對峰有 7 dB。但真正拿去判定的不是那些，
    是經過 GateMonitor 濾波後的值，殘餘抖動只有 0.5 dB 左右。

    這裡用的就是 GateMonitor 本身（跟 ui/app_gui.py 走同一條路徑），所以看到的
    就是系統真正在用的數字，不是另外算一套。推著車走來走去、或加了反射板之後
    開著這個看，最直接。
    """
    print("\n" + "=" * 78)
    print("即時監看（Ctrl+C 結束）")
    print("=" * 78)
    print(gate_monitor.describe())
    print()
    print("原始 = 最近一筆未處理的讀數（就是 drivers.ble_beacon_scanner 印的那種）")
    print("濾波 = GateMonitor 實際拿去判定的值")
    print("Δ    = 濾波後 內側 − 外側，正值代表偏內側。這個值翻轉就是一次穿越")
    print()

    raw = {gate_monitor.inside_beacon_id: None, gate_monitor.outside_beacon_id: None}
    last_print = 0.0
    events = []
    try:
        while True:
            try:
                obs = scanner.out_queue.get(timeout=0.2)
            except queue.Empty:
                obs = None
            if obs is not None:
                raw[obs.beacon_id] = obs.rssi
                crossing = gate_monitor.process_observation(obs.beacon_id, obs.rssi, obs.timestamp)
                if crossing:
                    events.append(crossing)
                    print(f"\n  *** 判定：{crossing} ***  (累計 {len(events)} 次)\n")

            now = time.time()
            if now - last_print < refresh_sec:
                continue
            last_print = now

            snap = gate_monitor.snapshot()
            ri = raw.get(gate_monitor.inside_beacon_id)
            ro = raw.get(gate_monitor.outside_beacon_id)

            def fmt(v, unit=""):
                return f"{v:>6.1f}{unit}" if v is not None else "   --  "

            if snap["diff"] is None:
                status = f"樣本不足（內 {snap['n_inside']} / 外 {snap['n_outside']}）"
            else:
                side = {"a": "內側", "b": "外側", None: "未定"}[snap["dominant"]]
                abs_ok = snap["above_abs_threshold"]
                abs_txt = "" if abs_ok is None else ("" if abs_ok else "  [訊號太弱，絕對門檻擋下]")
                status = f"判定={side}{abs_txt}"

            print(f"\r  原始 內{fmt(ri)} 外{fmt(ro)} │ "
                  f"濾波 內{fmt(snap['inside'])} 外{fmt(snap['outside'])} │ "
                  f"Δ={fmt(snap['diff'], ' dB')} │ {status}        ",
                  end="", flush=True)
    except KeyboardInterrupt:
        print(f"\n\n結束。這段期間共判定出 {len(events)} 次穿越：{events or '（沒有）'}")


def run_survey(scanner, inside_id, outside_id, offsets, seconds, method):
    """勘測模式：沿著行進方向站幾個位置，量出「差分值隨位置變化的梯度」，
    再跟實測雜訊比，直接告訴你目前這組佈署能不能可靠判定方向。

    這是用來回答「我加了反射板/改了擺放位置，到底有沒有變好」的——改之前跑一次、
    改之後再跑一次，比較『梯度/雜訊比』就知道，不用憑感覺猜。
    """
    positions = [
        ("門外 100 cm", "站在門外、距離門檻線約 1 公尺"),
        ("門外  50 cm", "往門口走一半"),
        ("門檻線   0 cm", "正好在門檻線上"),
        ("門內  50 cm", "跨進門內半公尺"),
        ("門內 100 cm", "門內 1 公尺"),
    ]
    print("\n" + "=" * 72)
    print("勘測模式：沿著行進方向量 5 個位置")
    print("=" * 72)
    print("每個位置都要維持推車實際使用時的姿態與朝向（車體金屬遮蔽影響很大）。")
    print("任何一個位置輸入 s 可以跳過。\n")

    results = []
    for label, hint in positions:
        raw = input(f"  [{label}] {hint}，準備好按 Enter（s = 跳過）：").strip().lower()
        if raw == "s":
            results.append((label, None))
            continue
        samples = collect(scanner, [inside_id, outside_id], seconds)
        ins = [v + offsets.get(inside_id, 0.0) for v in samples[inside_id]]
        outs = [v + offsets.get(outside_id, 0.0) for v in samples[outside_id]]
        if len(ins) < 5 or len(outs) < 5:
            print(f"    樣本太少（內側 {len(ins)}、外側 {len(outs)}），這個位置略過\n")
            results.append((label, None))
            continue
        delta = statistics.median(ins) - statistics.median(outs)
        noise = max(statistics.pstdev(ins), statistics.pstdev(outs))
        residual = max(residual_after_filter(ins, method),
                       residual_after_filter(outs, method))
        results.append((label, {
            "delta": delta, "noise": noise, "residual": residual,
            "inside": statistics.median(ins), "outside": statistics.median(outs),
            "n_in": len(ins), "n_out": len(outs),
        }))
        print(f"    內側={statistics.median(ins):>6.1f}　外側={statistics.median(outs):>6.1f}　"
              f"差分值={delta:+6.1f} dB　單筆雜訊={noise:.1f} dB\n")

    valid = [(l, r) for l, r in results if r is not None]
    if len(valid) < 2:
        print("有效位置不足 2 個，沒辦法算梯度。")
        return

    print("=" * 72)
    print(f"{'位置':<16}{'內側':>8}{'外側':>8}{'差分值':>10}{'筆數(內/外)':>14}")
    print("-" * 72)
    for label, r in valid:
        counts = f"{r['n_in']}/{r['n_out']}"
        print(f"{label:<16}{r['inside']:>8.1f}{r['outside']:>8.1f}{r['delta']:>+9.1f}dB{counts:>14}")

    deltas = [r["delta"] for _, r in valid]
    span = max(deltas) - min(deltas)
    noise = max(r["noise"] for _, r in valid)
    smoothed = max(r["residual"] for _, r in valid)
    ratio = span / smoothed if smoothed > 0 else float("inf")

    print("-" * 72)
    print(f"整段差分值變化幅度（最外到最內）  = {span:.1f} dB")
    print(f"單筆雜訊（各位置最大標準差）      = {noise:.1f} dB")
    print(f"經 {method} 濾波後的殘餘雜訊        = {smoothed:.1f} dB")
    print(f"梯度／雜訊比                      = {ratio:.1f}")

    print("\n判讀：")
    if ratio >= 8:
        print("  ✓ 很好。方向判定會很穩，遲滯可以設大一點換取抗雜訊能力。")
    elif ratio >= 4:
        print("  ○ 堪用。判定得出來，但邊界附近會有點猶豫，遲滯不要設太大。")
    else:
        print("  ✗ 不夠。位置造成的差異跟雜訊同量級，判定會不穩定或根本判不出來。")
        print("    優先順序：(1) 兩顆改成沿『行進方向』前後擺、間距 1~1.5 m，")
        print("              讓推車從兩顆中間穿過（這步免費，效果最大）")
        print("              (2) 每顆背後加金屬反射板做指向性（內朝內、外朝外）")
        print("              (3) 提高廣播頻率、加大平滑窗口，把雜訊壓下來")
        print("    注意：單純調低發射功率不會改善這個比值（見 README 的說明）。")

    # 中線附近的局部梯度才是判定真正用到的
    mid = next((r for l, r in valid if "門檻線" in l), None)
    near = [(l, r) for l, r in valid if "50 cm" in l]
    if mid is not None and near:
        local = max(abs(r["delta"] - mid["delta"]) for _, r in near)
        print(f"\n門檻線 ±50 cm 的局部梯度 = {local:.1f} dB")
        suggested_hyst = max(_HYSTERESIS_MIN_DB, min(local * 0.5, 2.0 * smoothed + 2))
        print(f"  建議 gate_hysteresis_dbm 設在 {suggested_hyst:.1f} 附近"
              f"（要蓋得過雜訊 {smoothed:.1f}，又不能超過局部梯度的一半）")
        if local < 2 * smoothed:
            print("  [警告] 局部梯度還不到雜訊的 2 倍——走到門口再走回來都可能判不出方向。")

    print()

def _main() -> int:
    parser = argparse.ArgumentParser(description="管制區門口雙 Beacon 校正（算 RSSI 偏移量與門檻值）")
    parser.add_argument("--seconds", type=float, default=15.0, help="每個位置收集幾秒樣本，預設 15")
    parser.add_argument("--adapter", default=None, help="藍牙介面名稱，預設用系統預設")
    parser.add_argument("--dry-run", action="store_true", help="只算給你看，不寫回 config.json")
    parser.add_argument("--monitor", action="store_true",
                        help="即時監看：同時顯示原始讀數與濾波後的值，以及判定結果。"
                             "推車走動或加了反射板之後開著這個看最直接")
    parser.add_argument("--survey", action="store_true",
                        help="勘測模式：沿行進方向量 5 個位置，算出梯度/雜訊比，"
                             "判斷目前佈署能不能可靠判定方向（改佈署前後各跑一次來比較）")
    args = parser.parse_args()

    cfg = load_landmark_config()
    pair = cfg.get("gate_beacon_pair", {})
    inside_id, outside_id = pair.get("inside_id"), pair.get("outside_id")
    if not inside_id or not outside_id:
        print("[錯誤] config.json 的 landmarks.gate_beacon_pair 沒有設定 inside_id / outside_id")
        return 1

    try:
        scanner = BleBeaconScanner(
            beacon_ids=load_beacon_ids(),
            address_map=load_beacon_identity_map(),
            out_queue=queue.Queue(),
            adapter=args.adapter,
        )
        scanner.start()
        scanner.wait_for_beacons([inside_id, outside_id], timeout_sec=20.0)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[錯誤] BLE 掃描器啟動失敗：{exc}")
        print("  1) sudo rfkill unblock bluetooth　2) pip install bleak")
        print("  3) 查實際位址/名稱：python3 -m drivers.ble_beacon_scanner --list\n")
        return 1

    try:
        if args.monitor:
            from core.gate_monitor import GateMonitor
            run_monitor(scanner, GateMonitor.from_config(cfg))
            return 0

        if args.survey:
            run_survey(scanner, inside_id, outside_id, cfg.get("rssi_offsets", {}),
                       args.seconds, cfg.get("rssi_estimator", "p75"))
            return 0

        # ------------------------------------------------------------------
        print("\n【步驟 1／2】把推車停在「門檻線」上")
        print("  也就是你希望系統判定為『剛好在門口正中間、內外未定』的那個位置。")
        print("  推車的姿態、Pi 的擺放方向都要跟平常實際使用時一樣（車體金屬會遮蔽訊號，")
        print("  拿在手上量跟裝在車上量結果會差很多）。")
        input("  準備好之後按 Enter 開始收集...")
        gate_samples = collect(scanner, [inside_id, outside_id], args.seconds)

        print("\n  門檻線上的量測結果：")
        gi = summarize(inside_id, gate_samples[inside_id])
        go = summarize(outside_id, gate_samples[outside_id])
        if gi is None or go is None:
            return 1

        # 校正偏移量：讓兩顆在門檻線上校正後相等。外側固定 0，全部補在內側。
        inside_offset = go["median"] - gi["median"]
        offsets = {inside_id: inside_offset, outside_id: 0.0}
        balanced_level = go["median"]

        print(f"\n  兩顆在同一個位置上差了 {gi['median'] - go['median']:+.1f} dB"
              f"（{inside_id} 減 {outside_id}）")
        print(f"  => {inside_id} 的 rssi_offset_db = {inside_offset:+.1f}")
        print(f"     {outside_id} 的 rssi_offset_db = 0.0（固定當基準）")
        if abs(inside_offset) < 2.0:
            print("     兩顆基準很接近，這組 Beacon 本來就蠻對稱的。")
        else:
            print(f"     差距不小——沒有這個校正的話，判定結果會偏向 "
                  f"{inside_id if inside_offset < 0 else outside_id} 那一側。")

        # 遲滯：要蓋得過平滑後的殘餘雜訊
        method = cfg.get("rssi_estimator", "p75")
        raw_sigma = max(gi["stdev"], go["stdev"])
        # 直接把實測樣本餵給真正在用的那個濾波器，量出殘餘抖動——不要用
        # 「σ/√N」這種只對平均數成立的公式去推估（p75 完全不適用）。
        smoothed_sigma = max(
            residual_after_filter(gate_samples[inside_id], method),
            residual_after_filter(gate_samples[outside_id], method),
        )
        hysteresis = max(_HYSTERESIS_MIN_DB, _HYSTERESIS_SIGMA_MULTIPLIER * smoothed_sigma)
        print(f"\n  實測單筆雜訊標準差最大 {raw_sigma:.1f} dB，"
              f"經 {method} 濾波後殘餘約 {smoothed_sigma:.1f} dB")
        print(f"  => 建議 gate_hysteresis_dbm = {hysteresis:.1f}"
              f"（約 2 倍殘餘雜訊，靜止時才不會反覆翻轉判定）")

        # 絕對門檻：站在門檻線上要過得了，離開門口範圍就過不了
        min_crossing = balanced_level - _ABS_THRESHOLD_MARGIN_DB
        print(f"\n  門檻線上校正後的訊號強度約 {balanced_level:.1f} dBm")
        print(f"  => 建議 gate_min_crossing_rssi_dbm = {min_crossing:.1f}"
              f"（再寬 {_ABS_THRESHOLD_MARGIN_DB:.0f} dB，避免正常通過反而被擋掉）")

        # ------------------------------------------------------------------
        print("\n【步驟 2／2】驗證：把推車推到店內深處（離門口最遠的地方）")
        print("  這一步是要確認「在店裡別處閒逛」不會被誤判成在門口。")
        print("  不想做可以直接輸入 s 跳過。")
        raw = input("  準備好之後按 Enter 開始收集（s = 跳過）：").strip().lower()
        if raw != "s":
            far_samples = collect(scanner, [inside_id, outside_id], args.seconds)
            print("\n  店內深處的量測結果（已套用上面算出的偏移量）：")
            ok = True
            for bid in (inside_id, outside_id):
                vals = [v + offsets[bid] for v in far_samples[bid]]
                if len(vals) < 5:
                    print(f"  {bid:<16} 只收到 {len(vals)} 筆——在這個位置收不到訊號，"
                          "這其實是好事（代表範圍夠侷限）")
                    continue
                med = statistics.median(vals)
                passes = med >= min_crossing
                print(f"  {bid:<16} 校正後中位數={med:>6.1f} dBm　"
                      f"{'⚠ 仍然超過絕對門檻' if passes else '✓ 低於絕對門檻，不會誤判'}")
                if passes:
                    ok = False
            if not ok:
                suggested = max(med, balanced_level - _ABS_THRESHOLD_MARGIN_DB) + 2.0
                print("\n  [警告] 在店內深處仍然過得了絕對門檻，代表 Beacon 的有效範圍太大。")
                print("  建議：(a) 把 Beacon 發射功率調更低（最有效），"
                      f"或 (b) 把 gate_min_crossing_rssi_dbm 收緊到 {suggested:.0f} 附近")
                print("  單純調軟體門檻的效果有限，優先調發射功率。")

        # ------------------------------------------------------------------
        print("\n" + "=" * 72)
        print("結論：")
        print(f"  landmarks.points[{inside_id}].rssi_offset_db  = {inside_offset:+.1f}")
        print(f"  landmarks.points[{outside_id}].rssi_offset_db = 0.0")
        print(f"  landmarks.gate_hysteresis_dbm                 = {hysteresis:.1f}")
        print(f"  landmarks.gate_min_crossing_rssi_dbm          = {min_crossing:.1f}")

        if args.dry_run:
            print("\n--dry-run：沒有寫回 config.json。")
            return 0
        if input("\n要寫回 config.json 嗎？(y 確認，其他任何輸入都不寫)：").strip().lower() != "y":
            print("沒有寫回。")
            return 0
        write_back(offsets, hysteresis, min_crossing)
        print("\n接下來：推車實際走一次進場與出場，確認 "
              "`python3 -m tools.run_real_hardware_flow` 會印出 entering / exiting。")
        return 0
    finally:
        scanner.stop()


if __name__ == "__main__":
    sys.exit(_main())
