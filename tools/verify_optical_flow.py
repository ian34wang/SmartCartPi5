"""
tools/verify_optical_flow.py

PMW3901 光流位移校正「驗證」工具——用來確認 config.json 裡目前的
odometry.optical_flow_px_to_mm 準不準，跟 calibrate_optical_flow.py
是不同用途：

    tools/calibrate_optical_flow.py  ->  算出新的 px_to_mm 並寫回 config.json
    tools/verify_optical_flow.py     ->  只讀 config.json 現有的值，實際推一段
                                          距離比對，不會修改 config.json

用法：
    python3 -m tools.verify_optical_flow
    python3 -m tools.verify_optical_flow --countdown 5
    python3 -m tools.verify_optical_flow --no-filter

流程：跟校正工具（v2）一樣，是「倒數（大字體、預設 5 秒）→ 按 Enter 開始
記錄 → 自己步調推車 → 按 Enter 停止記錄」，不是固定時間窗，推多久都行；
差別是這裡是拿 config.json 現有的 px_to_mm 把累積像素位移換算回『推測
距離』，再跟你事後輸入的實際距離比對算誤差百分比，不會動 config.json。
可以重複測多段距離，直到輸入 'q' 結束。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from drivers.uart_receiver import UartPacket, UartReceiver
from tools.calibrate_optical_flow import (
    big_countdown,
    filter_outliers,
    record_drag_until_stopped,
    sum_samples,
)

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


# ----------------------------------------------------------------------
# 純邏輯部分（可離線用假資料測試）
# ----------------------------------------------------------------------
def pixels_to_distance_mm(sum_dx: float, sum_dy: float, px_to_mm: float) -> float:
    total_pixels = math.hypot(sum_dx, sum_dy)
    return total_pixels * px_to_mm


def compute_error_pct(measured_mm: float, actual_mm: float) -> float:
    if actual_mm == 0:
        raise ValueError("實際距離不能是 0")
    return (measured_mm - actual_mm) / actual_mm * 100.0


# ----------------------------------------------------------------------
def load_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _prompt_optional_float(prompt: str) -> Optional[float]:
    raw = input(prompt).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print("輸入不是有效數字，視為跳過比對。")
        return None


def _drain(q: "queue.Queue[UartPacket]") -> None:
    """清空 queue 裡的舊資料，避免倒數期間殘留的封包混進這一輪累積量。"""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break


def _wait_for_enter_then_stop(stop_event: threading.Event) -> None:
    try:
        input()
    except EOFError:
        pass
    stop_event.set()


# ----------------------------------------------------------------------
def run_one_check(receiver: UartReceiver, countdown_sec: int, px_to_mm: float,
                   filter_enabled: bool, mad_multiplier: float) -> None:
    input("按 Enter 開始倒數，倒數結束後按自己的步調推車，推完按 Enter 停止記錄...")
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
    print(f"  收集結束，共收到 {raw_count} 筆封包{dropped_note}，sum_dx={sum_dx:g}, sum_dy={sum_dy:g}")
    if not filtered_samples:
        print("  沒有可用的樣本，這輪跳過。")
        return

    measured_mm = pixels_to_distance_mm(sum_dx, sum_dy, px_to_mm)
    print(f"  用目前的 px_to_mm={px_to_mm} 換算出推測距離 = {measured_mm:.1f} mm（{measured_mm / 10:.2f} cm）")

    actual_cm = _prompt_optional_float("  這段實際距離是幾公分？(直接按 Enter 跳過比對) ")
    if actual_cm is not None:
        error_pct = compute_error_pct(measured_mm, actual_cm * 10.0)
        print(f"  實際 = {actual_cm:.2f} cm，誤差 = {error_pct:+.2f}%")


def run_verification(port: str, baudrate: int, countdown_sec: int,
                      filter_enabled: bool = True, mad_multiplier: float = 6.0) -> int:
    config = load_config()
    odometry_cfg = config.get("odometry", {})
    px_to_mm = odometry_cfg.get("optical_flow_px_to_mm", 1.0)
    print(f"目前 config.json 的 odometry.optical_flow_px_to_mm = {px_to_mm}")

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
            return 1

        while True:
            try:
                run_one_check(receiver, countdown_sec, px_to_mm, filter_enabled, mad_multiplier)
            except ValueError as exc:
                print(f"  {exc}")

            again = input("\n按 Enter 測下一段距離，輸入 'q' 結束：").strip().lower()
            if again == "q":
                break
        return 0
    finally:
        receiver.stop()


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

    parser = argparse.ArgumentParser(description="PMW3901 光流位移校正驗證工具（只讀 config.json，不會寫回）")
    parser.add_argument("--port", default=default_port, help=f"UART 裝置節點（預設讀 config.json，目前為 {default_port}）")
    parser.add_argument("--baud", type=int, default=default_baud, help="鮑率")
    parser.add_argument("--countdown", type=int, default=5, help="開始記錄前的大倒數秒數，預設 5")
    parser.add_argument("--no-filter", action="store_true", help="關掉離群值過濾，全部樣本都拿來加總")
    parser.add_argument("--mad-multiplier", type=float, default=6.0, help="離群值過濾的門檻倍數，預設 6.0")
    args = parser.parse_args()

    try:
        return run_verification(
            port=args.port,
            baudrate=args.baud,
            countdown_sec=args.countdown,
            filter_enabled=not args.no_filter,
            mad_multiplier=args.mad_multiplier,
        )
    except KeyboardInterrupt:
        print("\n已中斷。")
        return 1


if __name__ == "__main__":
    sys.exit(_main())
