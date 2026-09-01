"""
tools/calibrate_weight.py

HX711 重量校正互動工具。

背景：
    weight.hx711_offset 和 weight.hx711_scale 是 config.json 裡標記
    CALIBRATE_ME 的兩個佔位值，需要在購物車重量感測結構（秤台、HX711
    模組）實際固定好之後，用「空秤」與「已知砝碼」各量一次才能算出來：

        offset = 空秤時 hx711_raw 的平均值
        scale  = (已知砝碼時 hx711_raw 的平均值 - offset) / 已知砝碼重量(g)

    之後量到任何 hx711_raw，換算公克數就是 (hx711_raw - offset) / scale。

目前狀態：
    購物車的秤重機構還在調整中，還沒辦法實際量測，所以這支工具目前
    「還沒有在真實硬體上跑過」。先把互動流程與計算邏輯做出來並用假資料
    驗證過，等結構穩定之後就能直接接上真的 UART 跑一次。

用法：
    python3 -m tools.calibrate_weight --port /dev/ttyAMA0
    python3 -m tools.calibrate_weight --dry-run          # 只算數值，不寫回 config.json
    python3 -m tools.calibrate_weight --samples 50 --timeout 10
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import time
from pathlib import Path
from typing import List, Optional

from drivers.uart_receiver import UartPacket, UartReceiver

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


# ----------------------------------------------------------------------
# 純邏輯部分（可離線用假資料測試，不需要真的 UART/硬體）
# ----------------------------------------------------------------------
def collect_hx711_samples(
    out_queue: "queue.Queue[UartPacket]",
    num_samples: int,
    timeout_sec: float,
    poll_interval_sec: float = 0.05,
) -> List[int]:
    """從 queue 收集 num_samples 筆 hx711_raw 原始值。

    在 timeout_sec 內收滿 num_samples 筆就提早結束；超過 timeout 只回傳
    當下收到的（可能不足 num_samples 筆，由呼叫端決定夠不夠用）。
    """
    samples: List[int] = []
    deadline = time.time() + timeout_sec
    while len(samples) < num_samples and time.time() < deadline:
        try:
            packet = out_queue.get(timeout=poll_interval_sec)
        except queue.Empty:
            continue
        samples.append(packet.hx711_raw)
    return samples


def compute_offset(zero_samples: List[int]) -> float:
    if not zero_samples:
        raise ValueError("沒有收到任何空秤樣本，無法計算 offset")
    return sum(zero_samples) / len(zero_samples)


def compute_scale(loaded_samples: List[int], offset: float, known_weight_g: float) -> float:
    if not loaded_samples:
        raise ValueError("沒有收到任何加砝碼樣本，無法計算 scale")
    if known_weight_g <= 0:
        raise ValueError("已知砝碼重量必須大於 0")
    loaded_mean = sum(loaded_samples) / len(loaded_samples)
    diff = loaded_mean - offset
    if diff == 0:
        raise ValueError("加砝碼前後 hx711_raw 平均值沒有變化，無法計算 scale（檢查砝碼是否真的放上去、HX711 接線是否正常）")
    return diff / known_weight_g


# ----------------------------------------------------------------------
# config.json 讀寫（保留其他欄位與註解，只更新 weight 區塊）
# ----------------------------------------------------------------------
def load_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def write_weight_calibration(offset: float, scale: float) -> None:
    config = load_config()
    weight_cfg = config.setdefault("weight", {})
    weight_cfg["hx711_offset"] = offset
    weight_cfg["hx711_scale"] = scale
    weight_cfg["_hx711_offset_comment"] = (
        f"已於 {time.strftime('%Y-%m-%d %H:%M:%S')} 用 tools/calibrate_weight.py 實測校正（空秤平均值）。"
    )
    weight_cfg["_hx711_scale_comment"] = (
        f"已於 {time.strftime('%Y-%m-%d %H:%M:%S')} 用 tools/calibrate_weight.py 實測校正"
        "（(加砝碼平均值 - offset) / 已知砝碼重量(g)）。"
    )
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")
    logger.info("已寫回 config.json：hx711_offset=%s, hx711_scale=%s", offset, scale)


# ----------------------------------------------------------------------
# 互動流程
# ----------------------------------------------------------------------
def run_calibration(
    port: str,
    baudrate: int,
    num_samples: int,
    timeout_sec: float,
    dry_run: bool,
) -> Optional[tuple]:
    receiver = UartReceiver(port=port, baudrate=baudrate)
    receiver.start()
    try:
        # 等第一筆封包，確認 UART 真的有資料進來，避免使用者空等 timeout。
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

        input("\n[步驟 1/2] 請確保秤台上『空秤』（沒有放任何東西），完成後按 Enter 開始量測...")
        _drain(receiver.out_queue)
        zero_samples = collect_hx711_samples(receiver.out_queue, num_samples, timeout_sec)
        print(f"  收到 {len(zero_samples)} 筆樣本。")
        offset = compute_offset(zero_samples)
        print(f"  offset = {offset:.2f}")

        known_weight_g = _prompt_float("\n請輸入已知砝碼的重量（公克）：")
        input(f"[步驟 2/2] 請把 {known_weight_g:g} 公克的砝碼放上秤台，完成後按 Enter 開始量測...")
        _drain(receiver.out_queue)
        loaded_samples = collect_hx711_samples(receiver.out_queue, num_samples, timeout_sec)
        print(f"  收到 {len(loaded_samples)} 筆樣本。")
        scale = compute_scale(loaded_samples, offset, known_weight_g)
        print(f"  scale = {scale:.6f}")

        print(f"\n計算結果：hx711_offset = {offset:.2f}, hx711_scale = {scale:.6f}")

        if dry_run:
            print("（--dry-run 模式，不寫回 config.json）")
            return (offset, scale)

        answer = input("是否要把這組結果寫回 config.json？[y/N] ").strip().lower()
        if answer == "y":
            write_weight_calibration(offset, scale)
            print("已寫回 config.json。")
        else:
            print("已取消，config.json 未變更。")
        return (offset, scale)
    finally:
        receiver.stop()


def _drain(q: "queue.Queue[UartPacket]") -> None:
    """清空 queue 裡的舊資料，避免上一個步驟殘留的樣本混進下一輪平均。"""
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

    parser = argparse.ArgumentParser(description="HX711 重量校正互動工具")
    parser.add_argument("--port", default=default_port, help=f"UART 裝置節點（預設讀 config.json，目前為 {default_port}）")
    parser.add_argument("--baud", type=int, default=default_baud, help="鮑率")
    parser.add_argument("--samples", type=int, default=30, help="每個階段要收集的樣本數（預設 30）")
    parser.add_argument("--timeout", type=float, default=10.0, help="每個階段收集樣本的逾時秒數（預設 10）")
    parser.add_argument("--dry-run", action="store_true", help="只計算結果並印出，不寫回 config.json")
    args = parser.parse_args()

    try:
        result = run_calibration(
            port=args.port,
            baudrate=args.baud,
            num_samples=args.samples,
            timeout_sec=args.timeout,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        print("\n已中斷。")
        return 1

    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(_main())
