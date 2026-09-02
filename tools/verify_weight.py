"""
tools/verify_weight.py

HX711 重量校正「驗證」工具——用來確認 config.json 裡目前的
weight.hx711_offset / weight.hx711_scale 準不準，跟 calibrate_weight.py
是不同用途：

    tools/calibrate_weight.py  ->  算出新的 offset/scale 並寫回 config.json
    tools/verify_weight.py     ->  只讀 config.json 現有的值，拿已知重量的
                                    物品實測比對，不會修改 config.json

用法：
    python3 -m tools.verify_weight                 # 讀 config.json 的 serial/weight 設定
    python3 -m tools.verify_weight --samples 30 --timeout 10
    python3 -m tools.verify_weight --port /dev/ttyAMA0 --baud 115200

流程：每一輪先讓秤台淨空收集一批樣本（純粹顯示目前讀到的『0 點』是否
還接近 offset，用來提早發現零點飄移），接著放上一個你知道實際重量的
物品、收集樣本、換算成公克，再輸入該物品的實際重量（可留空跳過比對），
程式會印出換算值、實際值與誤差百分比。可以重複測多個物品，直到輸入
'q' 結束。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

import queue

from core.weight_convert import raw_to_grams
from drivers.uart_receiver import UartPacket, UartReceiver
from tools.calibrate_weight import collect_hx711_samples

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


# ----------------------------------------------------------------------
# 純邏輯部分（可離線用假資料測試）
# ----------------------------------------------------------------------
# raw_to_grams 已搬到 core/weight_convert.py（Phase 4 的 cart_state_machine.py
# 也需要用同一個換算公式），這裡改成 import，保留同樣的函式名稱，呼叫方式
# 不用改。


def compute_error_pct(measured_g: float, actual_g: float) -> float:
    if actual_g == 0:
        raise ValueError("實際重量不能是 0")
    return (measured_g - actual_g) / actual_g * 100.0


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
    """清空 queue 裡的舊資料，避免上一輪殘留的樣本混進這一輪平均。"""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break


# ----------------------------------------------------------------------
def run_one_check(receiver: UartReceiver, num_samples: int, timeout_sec: float,
                   offset: float, scale: float) -> None:
    _drain(receiver.out_queue)
    samples: List[int] = collect_hx711_samples(receiver.out_queue, num_samples, timeout_sec)
    if not samples:
        print("  沒收到任何樣本，檢查 UART 連線。")
        return
    raw_mean = sum(samples) / len(samples)
    weight_g = raw_to_grams(raw_mean, offset, scale)
    print(f"  收到 {len(samples)} 筆樣本，raw 平均 = {raw_mean:.2f} -> 換算重量 = {weight_g:.2f} g")

    actual_g = _prompt_optional_float("  這個物品實際重量是幾公克？(直接按 Enter 跳過比對) ")
    if actual_g is not None:
        error_pct = compute_error_pct(weight_g, actual_g)
        print(f"  實際 = {actual_g:.2f} g，誤差 = {error_pct:+.2f}%")


def run_verification(port: str, baudrate: int, num_samples: int, timeout_sec: float) -> int:
    config = load_config()
    weight_cfg = config.get("weight", {})
    offset = weight_cfg.get("hx711_offset", 0)
    scale = weight_cfg.get("hx711_scale", 1.0)
    print(f"目前 config.json 的 weight.hx711_offset = {offset}, weight.hx711_scale = {scale}")

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
            input("\n請把要測試的物品放上秤台（或先淨空秤台測零點），完成後按 Enter 開始量測...")
            try:
                run_one_check(receiver, num_samples, timeout_sec, offset, scale)
            except ValueError as exc:
                print(f"  {exc}")

            again = input("\n按 Enter 測下一個物品，輸入 'q' 結束：").strip().lower()
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

    parser = argparse.ArgumentParser(description="HX711 重量校正驗證工具（只讀 config.json，不會寫回）")
    parser.add_argument("--port", default=default_port, help=f"UART 裝置節點（預設讀 config.json，目前為 {default_port}）")
    parser.add_argument("--baud", type=int, default=default_baud, help="鮑率")
    parser.add_argument("--samples", type=int, default=30, help="每次量測要收集的樣本數（預設 30）")
    parser.add_argument("--timeout", type=float, default=10.0, help="收集樣本的逾時秒數（預設 10）")
    args = parser.parse_args()

    try:
        return run_verification(
            port=args.port,
            baudrate=args.baud,
            num_samples=args.samples,
            timeout_sec=args.timeout,
        )
    except KeyboardInterrupt:
        print("\n已中斷。")
        return 1


if __name__ == "__main__":
    sys.exit(_main())
