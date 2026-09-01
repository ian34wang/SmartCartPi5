"""
tools/mock_uart_generator.py

沒有實體 MCU 時，用虛擬序列埠（PTY pair）灌假的 $SDK,... 封包，讓
drivers/uart_receiver.py 可以在不接硬體的情況下被測試（單元測試 / CI）。

注意：這不是「一定要先做的必要步驟」——目前韌體已經在實體硬體上跑通、
封包格式也雙向驗證過了。這支只是拿來做自動化測試/CI 用的輔助工具，
不擋在正式開發流程前面。

用法：
    # 建立一組虛擬序列埠，開始送假封包，印出讓測試程式連線用的 port 路徑
    python3 -m tools.mock_uart_generator

    # 送一些帶錯誤 checksum / 缺欄位的封包，測試 uart_receiver.py 的容錯
    python3 -m tools.mock_uart_generator --inject-errors

    # 在自己的 pytest / CI 腳本裡也可以直接 import 用：
    from tools.mock_uart_generator import MockUartGenerator
    gen = MockUartGenerator()
    gen.start()
    ...把 gen.client_port 填進 UartReceiver(port=...)...
    gen.stop()
"""

from __future__ import annotations

import argparse
import logging
import os
import pty
import random
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


def _checksum_hex(payload: str) -> str:
    cs = 0
    for ch in payload:
        cs ^= ord(ch)
    return f"{cs:02X}"


def build_packet(dx: int, dy: int, squal: int, yaw: float, pitch: float, roll: float, hx711_raw: int) -> str:
    body = f"SDK,{dx},{dy},{squal},{yaw:.1f},{pitch:.1f},{roll:.1f},{hx711_raw}"
    return f"${body}*{_checksum_hex(body)}\r\n"


@dataclass
class MockScenario:
    """簡單的假資料生成模式，可依需求擴充（例如模擬固定距離移動、模擬秤重變化）。"""

    name: str = "idle_jitter"
    base_yaw: float = 0.0
    hx711_baseline: int = -330000


class MockUartGenerator:
    """開一組 PTY pair，其中一端 (server) 自己餵資料，另一端 (client_port) 給
    UartReceiver 或其他測試程式當作序列埠路徑連線。
    """

    def __init__(
        self,
        rate_hz: float = 20.0,
        scenario: Optional[MockScenario] = None,
        inject_errors: bool = False,
        error_rate: float = 0.05,
    ):
        self.rate_hz = rate_hz
        self.scenario = scenario or MockScenario()
        self.inject_errors = inject_errors
        self.error_rate = error_rate

        self._master_fd: Optional[int] = None
        self.client_port: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> str:
        self._master_fd, slave_fd = pty.openpty()
        self.client_port = os.ttyname(slave_fd)
        os.close(slave_fd)  # UartReceiver 會自己重新開啟 client_port

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="MockUartGenerator", daemon=True)
        self._thread.start()
        logger.info("Mock UART 產生器啟動，虛擬序列埠：%s", self.client_port)
        return self.client_port

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def _run_loop(self) -> None:
        assert self._master_fd is not None
        period = 1.0 / self.rate_hz
        t = 0.0
        while not self._stop_event.is_set():
            dx = random.randint(-3, 3)
            dy = random.randint(-1, 1)
            squal = random.randint(60, 255)  # 模擬正常追蹤信心值；偶爾也可以自己改低模擬追蹤不良
            yaw = self.scenario.base_yaw + 5.0 * (t % 10 - 5) / 5.0
            pitch = random.uniform(-1.0, 1.0)
            roll = random.uniform(-1.0, 1.0)
            hx711_raw = self.scenario.hx711_baseline + random.randint(-50, 50)

            if self.inject_errors and random.random() < self.error_rate:
                # 故意送一個 checksum 錯誤的封包，測試接收端的容錯行為
                body = f"SDK,{dx},{dy},{squal},{yaw:.1f},{pitch:.1f},{roll:.1f},{hx711_raw}"
                line = f"${body}*FF\r\n"
            else:
                line = build_packet(dx, dy, squal, yaw, pitch, roll, hx711_raw)

            try:
                os.write(self._master_fd, line.encode())
            except OSError as exc:
                logger.error("寫入虛擬序列埠失敗：%s", exc)
                break

            t += period
            time.sleep(period)


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Mock UART 封包產生器（CI / 無硬體測試用）")
    parser.add_argument("--rate-hz", type=float, default=20.0)
    parser.add_argument("--inject-errors", action="store_true", help="隨機送出 checksum 錯誤的封包")
    parser.add_argument("--error-rate", type=float, default=0.05)
    parser.add_argument("--duration-sec", type=float, default=0.0, help="0 表示持續執行直到 Ctrl+C")
    args = parser.parse_args()

    gen = MockUartGenerator(
        rate_hz=args.rate_hz, inject_errors=args.inject_errors, error_rate=args.error_rate
    )
    port = gen.start()
    print(f"虛擬序列埠已建立：{port}")
    print("把這個路徑填進 UartReceiver(port=...) 或 config.json 的 serial.port 做測試。")
    print("(Ctrl+C 結束)")

    try:
        if args.duration_sec > 0:
            time.sleep(args.duration_sec)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        gen.stop()


if __name__ == "__main__":
    _main()
