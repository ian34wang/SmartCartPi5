"""
tools/calibrate_optical_flow.py

PMW3901 光流與 BNO080 航向的校正。錄音與互動在這裡，計算在 core/flow_calibration.py
（符號、公式、判讀標準都寫在那邊）。

一次錄完四件事，全部存成原始封包，事後可以用 --from-data 重算：

    1. 推車停著不動 5 秒                       → 靜止時光流會不會亂跳、yaw 漂移多快
    2. 沿地上一條直線往前推量好的距離，推好幾趟  → optical_flow_px_to_mm、感測器安裝角
    3. 往車子左側橫推一小段（選做）              → 感測器有沒有左右顛倒
    4. 原地向左（逆時針）轉 90°                 → yaw 的正負號

算出來的參數會丟進 core/odometry_engine.OdometryEngine（執行期同一份程式）重播每一趟，
比較推出來的距離與方向。

錄音存在 data/flow_calibration/<時間>/（samples.csv + samples.meta.json + result.json）。

會寫回 config.json 的 odometry 區塊：optical_flow_px_to_mm、flow_rotation_deg、flow_mirror、
yaw_sign，以及 flow_calibration_source。

準備：地上找一條直線（磁磚縫，或貼一條膠帶），量好一段距離，建議 3 m 以上；起點、終點
做記號。推的時候車頭對準直線、速度跟平常一樣。

用法：
    python3 -m tools.calibrate_optical_flow --dry-run          # 錄音、計算，不寫回
    python3 -m tools.calibrate_optical_flow                    # 同上，最後問要不要寫回
    python3 -m tools.calibrate_optical_flow --runs 5
    python3 -m tools.calibrate_optical_flow --from-data data/flow_calibration/20261116-160000

這支檔案裡的 big_countdown / filter_outliers / record_drag_until_stopped / sum_samples /
compute_px_to_mm 是給 tools/verify_optical_flow.py 用的。
"""

from __future__ import annotations

import argparse
import json
import math
import queue
import shutil
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.flow_calibration import GOOD_CV, FlowCalibration, calibrate  # noqa: E402
from core.uart_recording import (  # noqa: E402
    UartTap,
    countdown,
    load,
    new_session_dir,
    save_result,
    update_config,
    wait_for_stream,
)
from drivers.uart_receiver import UartPacket, UartReceiver  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO / "config.json"
_ROOT = _REPO / "data" / "flow_calibration"


def _rel(p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(_REPO))
    except ValueError:
        return str(p)


# ----------------------------------------------------------------------
# 純邏輯部分（可離線用假資料測試，不需要真的 UART/硬體）
# ----------------------------------------------------------------------
def record_drag_until_stopped(
    out_queue: "queue.Queue[UartPacket]",
    stop_event: threading.Event,
    poll_interval_sec: float = 0.05,
) -> List[Tuple[int, int]]:
    """持續收集封包的 (dx, dy)，直到 stop_event 被設定為止（不設時間上限）。

    呼叫前應該先把 queue 清空（見 _drain），避免上一輪殘留的舊封包混進來。
    stop_event 通常由另一條「等 Enter 按鍵」的執行緒觸發，讓使用者自己
    決定要推多久，不受固定時間窗限制。
    """
    samples: List[Tuple[int, int]] = []
    while not stop_event.is_set():
        try:
            packet = out_queue.get(timeout=poll_interval_sec)
        except queue.Empty:
            continue
        samples.append((packet.dx, packet.dy))
    # 使用者按下 Enter 到這裡之間，queue 可能還有幾筆剛好卡進來的封包，一併撈乾淨。
    while True:
        try:
            packet = out_queue.get_nowait()
        except queue.Empty:
            break
        samples.append((packet.dx, packet.dy))
    return samples


def filter_outliers(
    samples: List[Tuple[int, int]],
    mad_multiplier: float = 6.0,
) -> Tuple[List[Tuple[int, int]], int]:
    """用中位數絕對偏差（MAD）濾掉位移量明顯異常大的樣本。

    樣本數太少（<5）或大小幾乎一致（MAD=0）時直接不濾，回傳原始樣本，
    避免正常推車動作被誤判成離群值。回傳 (過濾後樣本, 被濾掉的筆數)。
    """
    if len(samples) < 5:
        return list(samples), 0
    magnitudes = [math.hypot(dx, dy) for dx, dy in samples]
    median = statistics.median(magnitudes)
    mad = statistics.median(abs(m - median) for m in magnitudes)
    if mad == 0:
        return list(samples), 0
    threshold = median + mad_multiplier * mad
    filtered = [s for s, m in zip(samples, magnitudes) if m <= threshold]
    dropped = len(samples) - len(filtered)
    return filtered, dropped


def sum_samples(samples: List[Tuple[int, int]]) -> Tuple[float, float]:
    sum_dx = float(sum(dx for dx, _ in samples))
    sum_dy = float(sum(dy for _, dy in samples))
    return sum_dx, sum_dy


def compute_px_to_mm(sum_dx: float, sum_dy: float, actual_distance_mm: float) -> float:
    if actual_distance_mm <= 0:
        raise ValueError("實測距離必須大於 0")
    total_pixels = math.hypot(sum_dx, sum_dy)
    if total_pixels == 0:
        raise ValueError("累積像素位移為 0，無法計算 px_to_mm（檢查拖曳期間感測器是否真的有在動、UART 是否正常收到封包）")
    return actual_distance_mm / total_pixels


# ----------------------------------------------------------------------
# 全螢幕大倒數（單純視覺效果，不影響任何計算邏輯）
# ----------------------------------------------------------------------
_DIGIT_PATTERNS = {
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "010", "010", "010", "010"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "001", "001", "001"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
}


def render_big_digit(digit_char: str, term_cols: int, term_rows: int) -> str:
    """把單一數字畫成置中、盡量填滿終端機畫面的大型 ASCII art。

    字元格通常是「高:寬 約 2:1」，所以橫向要用大約兩倍的字元數，畫出來的
    大方塊視覺上才會接近正方形，不會看起來被壓扁。
    """
    pattern = _DIGIT_PATTERNS.get(digit_char)
    if pattern is None:
        return digit_char

    pattern_rows = len(pattern)
    pattern_cols = len(pattern[0])

    max_scale_by_height = max(1, term_rows // pattern_rows)
    max_scale_by_width = max(1, term_cols // (pattern_cols * 2))
    scale_rows = max(1, min(max_scale_by_height, max_scale_by_width))
    scale_cols = scale_rows * 2

    art_lines: List[str] = []
    for row in pattern:
        line = "".join(("█" if ch == "1" else " ") * scale_cols for ch in row)
        art_lines.extend([line] * scale_rows)

    art_width = pattern_cols * scale_cols
    art_height = pattern_rows * scale_rows
    left_pad = max(0, (term_cols - art_width) // 2)
    top_pad = max(0, (term_rows - art_height) // 2)

    out_lines = [""] * top_pad
    out_lines.extend(" " * left_pad + line for line in art_lines)
    return "\n".join(out_lines)


def _get_terminal_size() -> Tuple[int, int]:
    try:
        size = shutil.get_terminal_size(fallback=(88, 69))
        return size.columns, size.lines
    except OSError:
        return 88, 69


def _clear_screen() -> None:
    print("\033[2J\033[H", end="")


def big_countdown(seconds: int) -> None:
    term_cols, term_rows = _get_terminal_size()
    for i in range(seconds, 0, -1):
        _clear_screen()
        print(render_big_digit(str(i), term_cols, term_rows))
        time.sleep(1.0)
    _clear_screen()


# ----------------------------------------------------------------------
# 錄音
# ----------------------------------------------------------------------
def _ask_float(prompt: str) -> float:
    while True:
        try:
            v = float(input(prompt).strip())
            if v > 0:
                return v
        except ValueError:
            pass
        print("    請輸入大於 0 的數字。")


def _live(tap: UartTap, stop: threading.Event) -> None:
    while not stop.is_set():
        last = tap.last
        if last is not None:
            print(f"\r    錄音中（{tap.count()} 筆）yaw={last.yaw_deg:+7.1f}°  squal={last.squal:3d}   ",
                  end="", flush=True)
        time.sleep(0.2)


def _until_enter(tap: UartTap, label: str, prompt: str, **info) -> None:
    tap.begin(label)
    stop = threading.Event()
    th = threading.Thread(target=_live, args=(tap, stop), daemon=True)
    th.start()
    input()
    stop.set()
    th.join(timeout=1.0)
    print()
    tap.end(**info)


def record(receiver, out_dir: Path, runs: int, static_sec: float) -> Optional[UartTap]:
    tap = UartTap(receiver, out_dir / "samples.csv", kind="flow_calibration", extra_meta={"runs": runs})
    tap.start()
    print("\n" + "=" * 80)
    print("光流與航向校正：靜止 → 直推幾趟 → 側推（選做）→ 原地左轉 90°")
    print("=" * 80)
    try:
        input(f"\n[1] 推車停好、不要碰，按 Enter（靜止 {static_sec:.0f} 秒）：")
        tap.begin("static")
        countdown(static_sec, tap, "靜止中")
        tap.end()

        print("\n[2] 直推：地上找一條直線（磁磚縫或膠帶），量好一段距離，建議 3 m 以上。")
        d_mm = _ask_float("    這段距離幾公分：") * 10.0
        for i in range(1, runs + 1):
            input(f"    第 {i}/{runs} 趟：車子停在起點、車頭對準直線，按 Enter 開始錄：")
            print("    推到終點停好，再按 Enter：")
            _until_enter(tap, f"fwd_{i}", "", distance_mm=d_mm)
            if i < runs:
                input("    把車子拉回起點（這段不錄），按 Enter 繼續：")

        print("\n[3] 側推（選做）：車頭方向不變，把車子往「車子的左邊」橫著推 50 cm 以上。")
        ans = input("    輪子能橫著走就按 Enter 開始錄（s = 跳過）：").strip().lower()
        if ans != "s":
            print("    推好停住，再按 Enter：")
            _until_enter(tap, "left_1", "")

        print("\n[4] 原地左轉：車頭對齊地上一條線，原地向左（逆時針，從上往下看）轉 90°，對齊另一條垂直的線。")
        input("    對齊好，按 Enter 開始錄：")
        print("    轉好停住，再按 Enter：")
        _until_enter(tap, "turn_left", "", angle_deg=90.0)
    except KeyboardInterrupt:
        tap.stop()
        print("\n中斷。已錄到的部分在", _rel(out_dir))
        return None
    tap.stop()
    return tap


# ----------------------------------------------------------------------
# 輸出、寫回
# ----------------------------------------------------------------------
def print_report(cal: FlowCalibration, odo: dict) -> None:
    if cal.error:
        print(f"\n[無法校正] {cal.error}")
        return
    print("\n" + "=" * 86)
    print("直推（S = 累積位移向量，k = D/|S|，θ = 感測器軸上的方向；重播 = 用算出的參數跑 OdometryEngine）")
    print(f"{'趟':<8}{'D (mm)':>8}{'|S| (count)':>13}{'k (mm/count)':>14}{'θ (°)':>8}{'Δyaw (°)':>10}"
          f"{'squal 中/5%':>13}{'重播誤差':>12}")
    print("-" * 86)
    for r in cal.runs:
        k = "—" if r.k is None else f"{r.k:.5f}"
        rep = "—" if r.replay_err_pct is None else f"{r.replay_err_pct:+.1f}% {r.replay_heading_err_deg:+.1f}°"
        print(f"{r.label:<8}{(r.distance_mm or 0):>8.0f}{r.counts:>13.0f}{k:>14}{r.theta_deg:>8.1f}"
              f"{r.yaw_change_deg:>10.1f}{f'{r.squal_median:.0f}/{r.squal_p5:.0f}':>13}{rep:>12}")
    cv = "—" if cal.cv is None else f"{cal.cv:.2%}"
    print(f"\n  k = {cal.px_to_mm:.5f} mm/count（各趟變異 {cv}，標準 ≤ {GOOD_CV:.0%}）")
    print(f"  直推方向各趟最大偏離 {cal.theta_spread_deg:.1f}°")
    if cal.lateral is not None:
        d = "—" if cal.lateral_delta_deg is None else f"{cal.lateral_delta_deg:+.0f}°"
        print(f"  側推方向 − 直推方向 = {d}  →  鏡像 {'是' if cal.mirror else '否' if cal.mirror is False else '判斷不出'}")
        if cal.lateral.replay_heading_err_deg is not None:
            print(f"    重播側推：方向誤差 {cal.lateral.replay_heading_err_deg:+.1f}°（應該在起點航向 +90°）")
    print(f"  安裝角 φ = {cal.rotation_deg:+.1f}°")
    if cal.turn_measured_deg is not None:
        print(f"  原地左轉 90°：yaw 變了 {cal.turn_measured_deg:+.1f}°  →  yaw_sign = {cal.yaw_sign}")
    if cal.static_flow_per_sec is not None:
        drift = cal.static_yaw_drift_deg_per_min
        print(f"  靜止時：光流 {cal.static_flow_per_sec:.1f} count/秒，yaw 漂移 "
              f"{'—' if drift is None else f'{drift:+.2f}°/分'}")

    print("\n判讀：")
    print(f"  {'✓ 可以' if cal.verdict == '可以' else '○ 勉強'}")
    for s in cal.issues:
        print(f"  - {s}")

    def tag(new, old):
        return "（不變）" if old == new else f"（原本 {old}）"

    print("\n建議參數：")
    print(f"  odometry.optical_flow_px_to_mm = {cal.px_to_mm:.5f}{tag(round(cal.px_to_mm, 5), odo.get('optical_flow_px_to_mm'))}")
    print(f"  odometry.flow_rotation_deg     = {cal.rotation_deg:+.1f}{tag(round(cal.rotation_deg, 1), odo.get('flow_rotation_deg'))}")
    print(f"  odometry.flow_mirror           = {str(bool(cal.mirror)).lower()}{tag(bool(cal.mirror), odo.get('flow_mirror'))}")
    print(f"  odometry.yaw_sign              = {cal.yaw_sign or 1}{tag(cal.yaw_sign or 1, odo.get('yaw_sign'))}")


def write_back(cal: FlowCalibration, source: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M")
    update_config(_CONFIG_PATH, "odometry", {
        "optical_flow_px_to_mm": round(cal.px_to_mm, 5),
        "flow_rotation_deg": round(cal.rotation_deg, 1),
        "flow_mirror": bool(cal.mirror),
        "yaw_sign": cal.yaw_sign or 1,
        "flow_calibration_source": source,
    }, {
        "_optical_flow_comment": f"{stamp} tools/calibrate_optical_flow.py：各趟直推 實際距離/累積位移長度 的平均（mm/count）。"
                                 "感測器高度或位置有改要重新校正。",
        "_flow_rotation_deg_comment": "PMW3901 安裝角：直推時感測器（去鏡像後）量到的方向角。core/odometry_engine.sensor_to_cart 用它把感測器軸轉成車身前/左。",
        "_flow_mirror_comment": "PMW3901 的軸是否左右顛倒（側推方向在直推方向 −90°）。",
        "_yaw_sign_comment": "BNO080 yaw 的正負號：原地左轉（逆時針）時 yaw 增加為 +1。yaw 是相對航向（開機為 0）。",
        "_flow_calibration_source_comment": "最後一次寫回光流參數的校正錄音，用 tools/calibrate_optical_flow.py --from-data 可以重算。",
    })
    print(f"\n已寫回 {_CONFIG_PATH}")


def finish(cal: FlowCalibration, out_dir: Path, dry_run: bool) -> int:
    cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    print_report(cal, cfg.get("odometry", {}))
    save_result(out_dir, {**{k: v for k, v in cal.__dict__.items() if k not in ("runs", "lateral")},
                          "runs": [r.__dict__ for r in cal.runs],
                          "lateral": None if cal.lateral is None else cal.lateral.__dict__})
    print(f"\n錄音與結果：{_rel(out_dir)}/")
    if cal.px_to_mm is None:
        return 1
    if dry_run:
        print("--dry-run：沒有寫回 config.json。")
        return 0
    if input("\n要寫回 config.json 嗎？(y 確認，其他任何輸入都不寫)：").strip().lower() != "y":
        print("沒有寫回。")
        return 0
    write_back(cal, _rel(out_dir))
    print("接下來：python3 -m core.odometry_engine 推一圈回到原點，看終點離原點多遠。")
    return 0


def _main() -> int:
    cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    ap = argparse.ArgumentParser(description="PMW3901 光流與 BNO080 航向校正")
    ap.add_argument("--dry-run", action="store_true", help="錄音、計算，不寫回 config.json")
    ap.add_argument("--from-data", default=None, help="用存下來的錄音重算（目錄或 samples.csv）")
    ap.add_argument("--runs", type=int, default=3, help="直推幾趟（預設 3）")
    ap.add_argument("--static-sec", type=float, default=5.0, help="靜止幾秒（預設 5）")
    ap.add_argument("--port", default=cfg.get("serial", {}).get("port", "/dev/ttyAMA0"))
    ap.add_argument("--baud", type=int, default=cfg.get("serial", {}).get("baudrate", 115200))
    args = ap.parse_args()

    if args.from_data:
        path = Path(args.from_data)
        out_dir = path if path.is_dir() else path.parent
        rec = load(path)
        print(f"讀入 {_rel(out_dir)}：{sum(len(v) for v in rec.samples.values())} 筆、{len(rec.segments)} 段")
        return finish(calibrate(rec), out_dir, args.dry_run)

    receiver = UartReceiver(port=args.port, baudrate=args.baud)
    receiver.start()
    try:
        print(f"等待 UART 資料流（{args.port}）...", flush=True)
        if not wait_for_stream(receiver):
            print(f"[錯誤] 5 秒內沒有收到任何 UART 封包：確認下位機有在送、接線、config.json 的 serial.port（{args.port}）。")
            return 1
        out_dir = new_session_dir(_ROOT)
        tap = record(receiver, out_dir, args.runs, args.static_sec)
    finally:
        receiver.stop()
    if tap is None:
        return 1
    return finish(calibrate(tap.recording()), out_dir, args.dry_run)


if __name__ == "__main__":
    sys.exit(_main())
