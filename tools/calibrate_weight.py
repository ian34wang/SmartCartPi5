"""
tools/calibrate_weight.py

HX711 秤重校正。錄音與互動在這裡，計算在 core/shopping/weight_calibration.py（說明、符號、
判讀標準都寫在那邊）。

一次錄完五件事，全部存成原始讀數，事後可以用 --from-data 重算：

    1. 空秤靜止                          → 零點、靜止雜訊
    2. 放上已知重量的砝碼（可以放好幾次） → hx711_offset、hx711_scale（最小平方）、線性
    3. 全部拿掉、空秤靜止                 → 零點漂移（潛變、機構沒回位）
    4. 籃子空的、照平常的方式推車          → 推車震動 → 未掃碼門檻 unscanned_change_threshold_g
    5. 掃碼後把砝碼像平常放商品一樣放進去  → 用執行期的狀態機重播，定「等讀數穩定」的參數

參數不是用公式推的：第 4、5 步的讀數直接丟進 core/shopping/state_machine.CartStateMachine
（跟 ui/app_gui.py 同一份程式）重播，看實際會不會誤報、會不會判錯。

錄音存在 data/weight_calibration/<時間>/（samples.csv + samples.meta.json + result.json）。

會寫回 config.json 的 weight 區塊：hx711_offset、hx711_scale、unscanned_change_threshold_g、
settle_window_sec、settle_band_g，以及 weight_calibration_source（用了哪一份錄音）。

**校正完要重新秤商品**：資料庫裡每個商品的 standard_weight_g 是用當時的 scale 量的，
scale 一改，那些數字就跟新的換算對不上——用 `python3 -m tools.product_admin edit <條碼>`
一個一個重秤。

用法：
    python3 -m tools.calibrate_weight --dry-run              # 錄音、計算，不寫回
    python3 -m tools.calibrate_weight                        # 同上，最後問要不要寫回
    python3 -m tools.calibrate_weight --static-sec 10 --push-sec 30 --drops 5
    python3 -m tools.calibrate_weight --from-data data/weight_calibration/20261116-150000
"""

from __future__ import annotations

import argparse
import queue
import sys
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from core.shopping.state_machine import load_state_machine_config  # noqa: E402
from core.recording import (  # noqa: E402
    UartTap,
    countdown,
    load,
    new_session_dir,
    save_result,
    segment_stats_line,
    wait_for_stream,
)
from core.shopping.weight_calibration import (  # noqa: E402
    PRODUCT_TOLERANCE_RATIO,
    T_FLOOR,
    WeightCalibration,
    calibrate,
)
from database.db_manager import DBManager  # noqa: E402
from drivers.uart_receiver import UartPacket, UartReceiver  # noqa: E402

_OUT_ROOT = config.DATA_DIR / "weight_calibration"



# ----------------------------------------------------------------------
# 給 tools/product_admin.py、tools/verify_weight.py 用（現場秤商品、驗證）
# ----------------------------------------------------------------------
def collect_hx711_samples(out_queue: "queue.Queue[UartPacket]", num_samples: int, timeout_sec: float,
                          poll_interval_sec: float = 0.05) -> List[int]:
    """從 queue 收 num_samples 筆 hx711_raw；逾時就回傳目前收到的。"""
    samples: List[int] = []
    deadline = time.time() + timeout_sec
    while len(samples) < num_samples and time.time() < deadline:
        try:
            packet = out_queue.get(timeout=poll_interval_sec)
        except queue.Empty:
            continue
        samples.append(packet.hx711_raw)
    return samples


# ----------------------------------------------------------------------
def _ask_mass(prompt: str, allow_empty: bool) -> Optional[float]:
    while True:
        raw = input(prompt).strip()
        if not raw and allow_empty:
            return None
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
        print("    請輸入大於 0 的公克數。")


def _static(tap: UartTap, label: str, seconds: float, text: str, **info) -> None:
    tap.begin(label)
    countdown(seconds, tap, text)
    seg = tap.end(**info)
    n, rate = segment_stats_line(tap.recording().get(seg.label))
    print(f"    收到 {n} 筆（{rate:.1f} 筆/秒）")


def record(receiver, out_dir: Path, static_sec: float, push_sec: float, drops: int) -> Optional[UartTap]:
    tap = UartTap(receiver, out_dir / "samples.csv", kind="weight_calibration",
                  extra_meta={"static_sec": static_sec, "push_sec": push_sec})
    tap.start()
    print("\n" + "=" * 80)
    print("秤重校正：空秤 → 砝碼（可放好幾次）→ 拿掉 → 推車 → 放入測試。每一步都會先說要做什麼。")
    print("=" * 80)
    try:
        input("\n[1] 籃子／秤台完全淨空、推車停好不要碰，按 Enter：")
        _static(tap, "zero_start", static_sec, "空秤靜止中，不要碰車")

        print("\n[2] 放砝碼。建議至少兩種不同重量，涵蓋平常一籃商品的範圍（例如 500 g、1000 g、2000 g）。")
        print("    每次輸入「現在秤台上砝碼的總重」，可以一直往上加；直接按 Enter 結束這一步。")
        i = 0
        while True:
            i += 1
            m = _ask_mass(f"    第 {i} 次：秤台上砝碼總重（g）：", allow_empty=i > 1)
            if m is None:
                break
            input(f"    放好 {m:g} g（放在籃子中間），手拿開，按 Enter：")
            _static(tap, f"load_{i}", static_sec, f"{m:g} g 靜止中", mass_g=m)

        input("\n[3] 把砝碼全部拿掉，手拿開，按 Enter：")
        _static(tap, "zero_end", static_sec, "空秤靜止中")

        if push_sec > 0:
            print(f"\n[4] 推車震動：籃子保持空的，按 Enter 後用平常的速度推車 {push_sec:.0f} 秒")
            input("    （直線、轉彎、過地板接縫、停下再起步都做一點），按 Enter 開始：")
            _static(tap, "push_empty", push_sec, "推車中")

        if drops > 0:
            print(f"\n[5] 放入測試 {drops} 趟：模擬「掃完碼、把商品放進籃子」。")
            m = _ask_mass("    放入測試用哪個砝碼（g，建議 200~500 g）：", allow_empty=False)
            for k in range(1, drops + 1):
                input(f"    第 {k}/{drops} 趟：籃子淨空，砝碼拿在手上、籃子上方，按 Enter，看到「放！」再放：")
                tap.begin(f"drop_{k}")
                time.sleep(1.0)
                print("    放！（像平常放商品一樣放進去，手拿開不要再碰）")
                time.sleep(6.0)
                tap.end(mass_g=m)
                if k < drops:
                    input("    拿出砝碼，按 Enter 繼續下一趟：")
            print("    放入測試完成，可以把砝碼拿出來了。")
    except KeyboardInterrupt:
        tap.stop()
        print("\n中斷。已錄到的部分在", config.rel(out_dir))
        return None
    tap.stop()
    return tap


# ----------------------------------------------------------------------
def print_report(cal: WeightCalibration, cfg_weight: dict) -> None:
    if cal.error:
        print(f"\n[無法校正] {cal.error}")
        return
    a, b = cal.offset, cal.scale
    print("\n" + "=" * 80)
    print("靜止點（丟掉每段前 1 秒，取中位數）與最小平方直線  r = a + b·m")
    print(f"{'段落':<12}{'砝碼 m (g)':>12}{'中位數 r':>14}{'σ_r':>9}{'換算 w (g)':>13}{'殘差 (g)':>10}")
    print("-" * 80)
    for p in cal.points:
        w = (p.level_raw - a) / b
        print(f"{p.label:<12}{p.mass_g:>12g}{p.level_raw:>14.1f}{p.sigma_raw:>9.1f}{w:>13.1f}{p.residual_g:>+10.2f}")
    print(f"\n  a（hx711_offset）= {a:.2f}　　b（hx711_scale）= {b:.6f} count/g")
    print(f"  靜止雜訊 σ_s = {cal.sigma_static_g:.2f} g")
    if cal.zero_drift_g is not None:
        print(f"  零點漂移（拿掉後 − 開始）= {cal.zero_drift_g:+.2f} g")

    cur_t = cfg_weight.get("unscanned_change_threshold_g")
    if cal.push:
        print(f"\n推車震動（空籃，{cal.push.n} 筆）：最大偏離 {cal.push.max_abs_g:.1f} g、"
              f"99% 在 {cal.push.p99_abs_g:.1f} g 內、σ {cal.push.std_g:.1f} g")
        if cal.t_min is not None:
            print(f"  用狀態機重播：T ≥ {cal.t_min} g 時推車不會誤報「未掃碼的重量變化」")
    if cal.t_rec is not None:
        print(f"  建議 T = {cal.t_rec} g（原本 {cur_t}）：{cal.t_reason}")
    if cal.settle_table and len(cal.settle_table) > 1:
        print(f"\n等讀數穩定（範圍 B_s = {cal.settle_band_g:g} g）：每個等待時間 W_s 都重新定 T、重播全部放入")
        print(f"  {'W_s (秒)':<10}{'T (g)':>7}{'放入正常':>10}{'平均加入時間':>14}")
        for r in cal.settle_table:
            lat = "—" if r["latency"] is None else f"{r['latency']:.1f} 秒"
            mark = "  ← 選這個" if r["window"] == cal.settle_window_sec else ""
            name = "0（不等）" if r["window"] == 0 else f"{r['window']:g}"
            print(f"  {name:<10}{str(r['t_rec']):>7}{f'{r['ok']}/{r['n']}':>10}{lat:>14}{mark}")
    if cal.drops:
        w_txt = "" if cal.settle_window_sec is None else f"，W_s = {cal.settle_window_sec:g} 秒"
        print(f"\n放入測試（用狀態機重播：掃碼 → 放入，容差 {PRODUCT_TOLERANCE_RATIO:.0%}，T = {cal.t_rec} g{w_txt}）")
        for d in cal.drops:
            ok = d.outcome == "正常加入" and not d.extra_alerts
            when = "" if d.commit_after_sec is None else f"，掃碼後 {d.commit_after_sec:.1f} 秒加入"
            extra = f"，之後又誤報未掃碼 {d.extra_alerts} 次" if d.extra_alerts else ""
            print(f"  {'✓' if ok else '✗'} {d.label}：{d.outcome}{when}{extra}；衝擊超出穩定值 {d.overshoot_g:.1f} g")
            print(f"      {d.detail}")
    if cal.light_products:
        print(f"\n  比 T 還輕的商品（不掃碼直接放進去偵測不到）：{'、'.join(cal.light_products)}")

    print("\n判讀：")
    mark = {"可以": "✓ 可以", "勉強": "○ 勉強", "不夠": "✗ 不夠"}.get(cal.verdict, cal.verdict)
    print(f"  {mark}")
    for s in cal.issues:
        print(f"  - {s}")

    def tag(new, old):
        return "（不變）" if old == new else f"（原本 {old}）"

    print("\n建議參數：")
    print(f"  weight.hx711_offset                 = {a:.2f}{tag(round(a, 2), cfg_weight.get('hx711_offset'))}")
    print(f"  weight.hx711_scale                  = {b:.6f}{tag(round(b, 6), cfg_weight.get('hx711_scale'))}")
    if cal.t_rec is not None:
        print(f"  weight.unscanned_change_threshold_g = {cal.t_rec}{tag(cal.t_rec, cur_t)}")
    if cal.settle_window_sec is not None:
        print(f"  weight.settle_window_sec            = {cal.settle_window_sec:g}"
              f"{tag(cal.settle_window_sec, cfg_weight.get('settle_window_sec'))}")
        print(f"  weight.settle_band_g                = {cal.settle_band_g:g}"
              f"{tag(cal.settle_band_g, cfg_weight.get('settle_band_g'))}")


def result_dict(cal: WeightCalibration) -> dict:
    return {"verdict": cal.verdict, "error": cal.error, "offset": cal.offset, "scale": cal.scale,
            "sigma_static_g": cal.sigma_static_g, "zero_drift_g": cal.zero_drift_g,
            "points": [p.__dict__ for p in cal.points], "push": cal.push and cal.push.__dict__,
            "t_min": cal.t_min, "t_rec": cal.t_rec, "drops": [d.__dict__ for d in cal.drops],
            "settle_window_sec": cal.settle_window_sec, "settle_band_g": cal.settle_band_g,
            "settle_table": [{k: v for k, v in r.items() if k != "drops"} for r in cal.settle_table],
            "light_products": cal.light_products, "issues": cal.issues}


def write_back(cal: WeightCalibration, source: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M")
    values = {"hx711_offset": round(cal.offset, 2), "hx711_scale": round(cal.scale, 6),
              "weight_calibration_source": source}
    comments = {
        "_hx711_offset_comment": f"{stamp} tools/calibrate_weight.py 多點最小平方的截距（空秤讀數）。只影響顯示的絕對重量，比對看的是重量變化。",
        "_hx711_scale_comment": f"{stamp} tools/calibrate_weight.py 多點最小平方的斜率（count/g）。改了這個值，資料庫裡的商品標準重量要重秤。",
        "_weight_calibration_source_comment": "最後一次寫回秤重參數的校正錄音，用 tools/calibrate_weight.py --from-data 加這個路徑可以重算。",
    }
    if cal.t_rec is not None:
        values["unscanned_change_threshold_g"] = cal.t_rec
        comments["_unscanned_change_threshold_comment"] = (
            f"{stamp} tools/calibrate_weight.py：{cal.t_reason}。推車震動是丟進 CartStateMachine 重播找的最小不誤報門檻。"
            "比這個輕的商品不掃碼放進去偵測不到。")
    if cal.settle_window_sec is not None:
        values["settle_window_sec"] = cal.settle_window_sec
        values["settle_band_g"] = cal.settle_band_g
        comments["_settle_comment"] = (
            f"{stamp} tools/calibrate_weight.py：秤重比對、未掃碼偵測只用「穩定值」——最近 settle_window_sec 秒的讀數"
            " 最大−最小 ≤ settle_band_g 時取中位數（core/shopping/state_machine.py 開頭有定義）。band = max(2, 6σ_s)；"
            "window 是放入錄音重播時每一趟都正常加入、之後不誤報的最小值。0 = 不等（舊版行為）。")
    config.update_section("weight", values, comments)
    print(f"\n已寫回 {config.rel(config.CONFIG_PATH)}")


def _products() -> dict:
    try:
        db = DBManager()
        if not db.db_path.exists():
            return {}
        return {p.name: p.standard_weight_g for p in db.list_products()}
    except Exception:  # noqa: BLE001 — 只是用來提醒，讀不到不影響校正
        return {}


def finish(cal: WeightCalibration, out_dir: Path, dry_run: bool) -> int:
    cfg = config.load()
    print_report(cal, cfg.get("weight", {}))
    save_result(out_dir, result_dict(cal))
    print(f"\n錄音與結果：{config.rel(out_dir)}/")
    if cal.offset is None:
        return 1
    if dry_run:
        print("--dry-run：沒有寫回 config.json。")
        return 0
    if input("\n要寫回 config.json 嗎？(y 確認，其他任何輸入都不寫)：").strip().lower() != "y":
        print("沒有寫回。")
        return 0
    write_back(cal, config.rel(out_dir))
    return 0


def _main() -> int:
    cfg = config.load()
    ap = argparse.ArgumentParser(description="HX711 秤重校正（多點、雜訊、推車震動、放入測試）")
    ap.add_argument("--dry-run", action="store_true", help="錄音、計算，不寫回 config.json")
    ap.add_argument("--from-data", default=None, help="用存下來的錄音重算（目錄或 samples.csv）")
    ap.add_argument("--static-sec", type=float, default=8.0, help="每個靜止步驟幾秒（預設 8）")
    ap.add_argument("--push-sec", type=float, default=20.0, help="推車震動錄幾秒（預設 20）")
    ap.add_argument("--drops", type=int, default=3, help="放入測試幾趟（預設 3，0 = 不做）")
    ap.add_argument("--quick", action="store_true",
                    help="只做空秤、砝碼、拿掉三步（秤台沒動過、只是重新校正 scale 時用；門檻沿用原本的）")
    ap.add_argument("--t-floor", type=int, default=T_FLOOR, help=f"未掃碼門檻的人為下限（g，預設 {T_FLOOR}）")
    ap.add_argument("--port", default=cfg.get("serial", {}).get("port", "/dev/ttyAMA0"))
    ap.add_argument("--baud", type=int, default=cfg.get("serial", {}).get("baudrate", 115200))
    args = ap.parse_args()

    sm_cfg = load_state_machine_config()
    if args.from_data:
        path = Path(args.from_data)
        rec = load(path)
        out_dir = path if path.is_dir() else path.parent
        print(f"讀入 {config.rel(out_dir)}：{sum(len(v) for v in rec.samples.values())} 筆、{len(rec.segments)} 段")
        return finish(calibrate(rec, sm_cfg, _products(), args.t_floor), out_dir, args.dry_run)

    receiver = UartReceiver.from_config(cfg, port=args.port, baudrate=args.baud)
    receiver.start()
    try:
        print(f"等待 UART 資料流（{args.port}）...", flush=True)
        if not wait_for_stream(receiver):
            print(f"[錯誤] 5 秒內沒有收到任何 UART 封包：確認下位機有在送、接線、config.json 的 serial.port（{args.port}）。")
            return 1
        out_dir = new_session_dir(_OUT_ROOT)
        push_sec, drops = (0.0, 0) if args.quick else (args.push_sec, args.drops)
        tap = record(receiver, out_dir, args.static_sec, push_sec, drops)
    finally:
        receiver.stop()
    if tap is None:
        return 1
    print("\n計算中（推車與放入的讀數會丟進狀態機重播很多次）...", flush=True)
    return finish(calibrate(tap.recording(), sm_cfg, _products(), args.t_floor), out_dir, args.dry_run)


if __name__ == "__main__":
    sys.exit(_main())
