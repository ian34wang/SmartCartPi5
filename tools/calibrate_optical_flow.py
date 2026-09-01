"""
tools/calibrate_optical_flow.py

PMW3901 光流感測器「像素位移 -> 實際公釐數」校正互動工具。

背景：
    odometry.optical_flow_px_to_mm 是 config.json 裡標記 CALIBRATE_ME 的
    佔位值，跟感測器安裝高度、鏡頭視角、CPI 設定有關，沒辦法用理論公式
    算出來，需要實際拖車測試：

        1. 在地板上量一段已知距離（例如用捲尺量 100 公分）。
        2. 把購物車沿這段距離推過去，同時累計 PMW3901 回報的 dx/dy。
        3. total_pixels = sqrt(sum_dx^2 + sum_dy^2)
        4. px_to_mm = 實際距離(mm) / total_pixels

    因為使用者推車的當下沒辦法同時打字，這支工具用「倒數 + 固定時間窗
    自動收集」的方式，而不是用 Enter 鍵手動標記起訖。

目前狀態：
    購物車結構還在調整中，PMW3901 安裝位置/高度還沒定案，所以這支工具
    目前「還沒有在真實硬體上跑過」。先把互動流程與計算邏輯做出來並用假
    資料驗證過，等結構穩定之後就能直接接上真的 UART 跑一次。

用法：
    python3 -m tools.calibrate_optical_flow --port /dev/ttyAMA0
    python3 -m tools.calibrate_optical_flow --dry-run
    python3 -m tools.calibrate_optical_flow --duration 5 --countdown 3
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import queue
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from drivers.uart_receiver import UartPacket, UartReceiver

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


# ----------------------------------------------------------------------
# 純邏輯部分（可離線用假資料測試，不需要真的 UART/硬體）
# ----------------------------------------------------------------------
def record_drag(
    out_queue: "queue.Queue[UartPacket]",
    duration_sec: float,
    poll_interval_sec: float = 0.05,
) -> Tuple[float, float, int]:
    """在固定時間窗內累計 queue 裡所有封包的 dx/dy。

    回傳 (sum_dx, sum_dy, count)。呼叫前應該先把 queue 清空（見 _drain），
    避免計時開始前殘留的舊封包混進累計值。
    """
    sum_dx = 0.0
    sum_dy = 0.0
    count = 0
    deadline = time.time() + duration_sec
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            packet = out_queue.get(timeout=min(poll_interval_sec, remaining))
        except queue.Empty:
            continue
        sum_dx += packet.dx
        sum_dy += packet.dy
        count += 1
    return sum_dx, sum_dy, count


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
# 互動流程
# ----------------------------------------------------------------------
def run_calibration(
    port: str,
    baudrate: int,
    duration_sec: float,
    countdown_sec: int,
    dry_run: bool,
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

        actual_distance_mm = _prompt_float(
            "\n請先在地板上量一段直線距離（用捲尺），輸入實際距離（公分）："
        ) * 10.0

        print(
            f"\n準備好後，倒數結束（{countdown_sec} 秒）就開始收集資料，"
            f"請在收集期間（{duration_sec:g} 秒）把購物車沿剛剛量好的距離推過去。"
        )
        input("按 Enter 開始倒數...")
        for i in range(countdown_sec, 0, -1):
            print(f"  {i}...")
            time.sleep(1.0)
        print("  開始！推車吧！")

        _drain(receiver.out_queue)
        sum_dx, sum_dy, count = record_drag(receiver.out_queue, duration_sec)
        print(f"\n收集結束，共收到 {count} 筆封包，sum_dx={sum_dx}, sum_dy={sum_dy}")

        if count == 0:
            print("沒有收到任何封包，無法計算，請重試。")
            return None

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
    parser.add_argument("--duration", type=float, default=5.0, help="推車時的資料收集時間窗（秒），預設 5")
    parser.add_argument("--countdown", type=int, default=3, help="開始收集前的倒數秒數，預設 3")
    parser.add_argument("--dry-run", action="store_true", help="只計算結果並印出，不寫回 config.json")
    args = parser.parse_args()

    try:
        result = run_calibration(
            port=args.port,
            baudrate=args.baud,
            duration_sec=args.duration,
            countdown_sec=args.countdown,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        print("\n已中斷。")
        return 1

    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(_main())
