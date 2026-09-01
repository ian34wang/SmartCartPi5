"""
tools/calibrate_optical_flow.py

PMW3901 光流感測器「像素位移 -> 實際公釐數」校正互動工具。

背景：
    odometry.optical_flow_px_to_mm 是 config.json 裡標記 CALIBRATE_ME 的
    佔位值，跟感測器安裝高度、鏡頭視角、CPI 設定有關，沒辦法用理論公式
    算出來，需要實際拖車測試：

        1. 把購物車推過一段距離，同時累計 PMW3901 回報的 dx/dy。
        2. total_pixels = sqrt(sum_dx^2 + sum_dy^2)
        3. px_to_mm = 實際距離(mm) / total_pixels

    v2 改動（原本的「倒數 + 固定時間窗自動收集」流程不好用，人手動推車
    沒辦法剛好在固定秒數內走完固定距離，不是太快就是太慢）：現在不再
    強制「距離跟時間都要對得剛剛好」——只保留一個 5 秒的視覺化大倒數
    當「準備」的緩衝，倒數結束後開始記錄，記錄時間完全由使用者自己
    掌控：按 Enter 開始記錄後直接去推車，推完了再按一次 Enter 停止記錄，
    然後才輸入這段距離實際量到多少（可以是推之前先量好的，也可以是
    推完之後才量的，都可以）。

    整合方式：每一筆封包的 dx/dy 本身就是「這個取樣週期的相對位移」
    （不是累積值，見 uart_receiver.py 的封包格式說明），所以直接把
    收到的所有封包的 dx/dy 累加（離散版的積分／黎曼和）就是整段時間
    的總位移，不需要再乘取樣間隔。

    離群值處理：如果某幾筆封包的位移量明顯比其他筆大很多（PMW3901
    在追蹤不穩、光線不足、離地高度不對時偶爾會吐出異常值），這種單筆
    離群值不會像雜訊一樣互相抵銷，會直接偏移整段的加總，所以預設會用
    中位數絕對偏差（MAD）過濾掉明顯異常的樣本再加總；不想濾可以加
    --no-filter。

目前狀態：
    硬體（PMW3901、UART）本身是接好、可以跑的，只是購物車結構
    （PMW3901 安裝位置、離地高度）還在調整中——安裝高度會直接影響
    px_to_mm 的比例，所以現在跑出來的值只對「這個當下的安裝方式」準，
    結構之後再調整，很可能就要重新校正一次，不是一勞永逸的最終值。
    所以現在可以照下面用法實際跑跑看（建議先用 --dry-run 只看數字、
    不寫回 config.json），等安裝位置真正定案後，再正式跑一次把結果
    寫回 config.json。

用法：
    python3 -m tools.calibrate_optical_flow --port /dev/ttyAMA0
    python3 -m tools.calibrate_optical_flow --dry-run
    python3 -m tools.calibrate_optical_flow --countdown 5
    python3 -m tools.calibrate_optical_flow --no-filter    # 關掉離群值過濾
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import queue
import shutil
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from drivers.uart_receiver import UartPacket, UartReceiver

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


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
# config.json 讀寫（保留其他欄位與註解，只更新 odometry 區塊）
# ----------------------------------------------------------------------
def load_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def write_optical_flow_calibration(px_to_mm: float) -> None:
    config = load_config()
    odometry_cfg = config.setdefault("odometry", {})
    odometry_cfg["optical_flow_px_to_mm"] = px_to_mm
    odometry_cfg["_optical_flow_comment"] = (
        f"已於 {time.strftime('%Y-%m-%d %H:%M:%S')} 用 tools/calibrate_optical_flow.py 實測校正"
        "（實測距離(mm) / 拖曳期間累積像素位移）。安裝高度或感測器有更動時需要重新校正。"
    )
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")
    logger.info("已寫回 config.json：optical_flow_px_to_mm=%s", px_to_mm)


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
# 互動流程
# ----------------------------------------------------------------------
def _wait_for_enter_then_stop(stop_event: threading.Event) -> None:
    try:
        input()
    except EOFError:
        pass
    stop_event.set()


def run_calibration(
    port: str,
    baudrate: int,
    countdown_sec: int,
    dry_run: bool,
    filter_enabled: bool = True,
    mad_multiplier: float = 6.0,
) -> Optional[float]:
    receiver = UartReceiver(port=port, baudrate=baudrate)
    receiver.start()
    try:
        print("等待 UART 連線與資料流...")
        deadline = time.time() + 5.0
        got_first = False
        while time.time() < deadline:
            if not receiver.out_queue.empty():
                got_first = True
                break
            time.sleep(0.1)
        if not got_first:
            print("5 秒內沒有收到任何封包，請確認接線與 config.json 的 serial.port。")
            return None

        print(
            "\n等一下倒數結束就會開始記錄，記錄期間你可以用自己的步調把車推過去，"
            "不用趕——推完之後按 Enter 停止記錄，再告訴我實際推了多遠（可以是先量好"
            "的距離，也可以是推完才量的）。"
        )
        input("按 Enter 開始倒數...")
        big_countdown(countdown_sec)
        print("開始記錄！請把車推過去，推完後按 Enter 停止記錄...")

        _drain(receiver.out_queue)
        stop_event = threading.Event()
        stop_thread = threading.Thread(target=_wait_for_enter_then_stop, args=(stop_event,), daemon=True)
        stop_thread.start()
        samples = record_drag_until_stopped(receiver.out_queue, stop_event)
        stop_thread.join(timeout=1.0)

        raw_count = len(samples)
        if filter_enabled:
            filtered_samples, dropped = filter_outliers(samples, mad_multiplier)
        else:
            filtered_samples, dropped = samples, 0
        sum_dx, sum_dy = sum_samples(filtered_samples)

        dropped_note = f"，濾掉 {dropped} 筆離群值" if dropped else ""
        print(f"\n記錄結束，共收到 {raw_count} 筆封包{dropped_note}，sum_dx={sum_dx:g}, sum_dy={sum_dy:g}")

        if not filtered_samples:
            print("沒有可用的樣本，無法計算，請重試。")
            return None

        actual_distance_mm = _prompt_float("\n這段推車的實際距離是幾公分？") * 10.0

        px_to_mm = compute_px_to_mm(sum_dx, sum_dy, actual_distance_mm)
        print(f"\n計算結果：optical_flow_px_to_mm = {px_to_mm:.6f}")

        if dry_run:
            print("（--dry-run 模式，不寫回 config.json）")
            return px_to_mm

        answer = input("是否要把這個結果寫回 config.json？[y/N] ").strip().lower()
        if answer == "y":
            write_optical_flow_calibration(px_to_mm)
            print("已寫回 config.json。")
        else:
            print("已取消，config.json 未變更。")
        return px_to_mm
    finally:
        receiver.stop()


def _drain(q: "queue.Queue[UartPacket]") -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break


def _prompt_float(prompt: str) -> float:
    while True:
        raw = input(prompt).strip()
        try:
            value = float(raw)
            if value > 0:
                return value
            print("請輸入大於 0 的數字。")
        except ValueError:
            print("請輸入有效的數字。")


# ----------------------------------------------------------------------
def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    default_port = "/dev/ttyAMA0"
    default_baud = 115200
    try:
        cfg = load_config()
        default_port = cfg.get("serial", {}).get("port", default_port)
        default_baud = cfg.get("serial", {}).get("baudrate", default_baud)
    except (OSError, json.JSONDecodeError):
        pass

    parser = argparse.ArgumentParser(description="PMW3901 光流位移校正互動工具")
    parser.add_argument("--port", default=default_port, help=f"UART 裝置節點（預設讀 config.json，目前為 {default_port}）")
    parser.add_argument("--baud", type=int, default=default_baud, help="鮑率")
    parser.add_argument("--countdown", type=int, default=5, help="開始記錄前的大倒數秒數，預設 5")
    parser.add_argument("--dry-run", action="store_true", help="只計算結果並印出，不寫回 config.json")
    parser.add_argument("--no-filter", action="store_true", help="關掉離群值過濾，全部樣本都拿來加總")
    parser.add_argument("--mad-multiplier", type=float, default=6.0, help="離群值過濾的門檻倍數，預設 6.0（越小濾得越兇）")
    args = parser.parse_args()

    try:
        result = run_calibration(
            port=args.port,
            baudrate=args.baud,
            countdown_sec=args.countdown,
            dry_run=args.dry_run,
            filter_enabled=not args.no_filter,
            mad_multiplier=args.mad_multiplier,
        )
    except KeyboardInterrupt:
        print("\n已中斷。")
        return 1

    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(_main())
