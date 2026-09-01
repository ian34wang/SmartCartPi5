"""
drivers/uart_receiver.py

SAM-IoT Wx v2 (SAMD21) 的 UART 遙測封包接收器，正式專案版本。

這支是把已經在硬體上實測驗證過的 pi_uart_receiver.py（單執行緒、print 版本）
重構成背景執行緒 + Thread-safe Queue 的架構，供 main.py 的生產者-消費者模型使用：
本模組是「生產者」，把驗證過 checksum 的合法封包丟進 queue.Queue，core/odometry_engine.py
等消費者從 queue 裡取資料，兩者之間不直接呼叫，避免阻塞。

封包格式（已定案，MCU 端 20Hz 固定送出，沒有新資料就重送上一次的值）：
    $SDK,<dx>,<dy>,<yaw_deg>,<pitch_deg>,<roll_deg>,<hx711_raw>*<CS>\\r\\n

    dx, dy       int    PMW3901 相對位移（不是累積值）
    yaw/pitch/roll  float  BNO080 四元數轉換後的角度，1位小數
    hx711_raw    int32  未校正的原始 ADC 值（Offset/Scale 校正在 Pi 端做，見 config.json）
    CS           hex    2 位十六進位，$ 和 * 之間所有字元的 XOR（NMEA 算法）

重要：config.json 的 serial.port 必須是實測驗證過的裝置節點（本專案是
/dev/ttyAMA0），不要相信 /dev/serial0 這個別名——Pi 5 (RP1) 上它可能指向
完全不同的 UART 控制器。詳見 config.json 裡的註解與交接文件。

用法（獨立測試，行為類似原本的 pi_uart_receiver.py）：
    python3 -m drivers.uart_receiver
    python3 -m drivers.uart_receiver --port /dev/ttyAMA0 --csv-log
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import serial

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


@dataclass
class UartPacket:
    dx: int
    dy: int
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    hx711_raw: int
    timestamp: float  # time.time()，收到這個封包當下的本機時間戳記

    def to_csv_row(self) -> list:
        return [self.timestamp, self.dx, self.dy, self.yaw_deg, self.pitch_deg,
                self.roll_deg, self.hx711_raw]


# ----------------------------------------------------------------------
# 封包解析（沿用 pi_uart_receiver.py 已驗證過的邏輯，維持行為一致）
# ----------------------------------------------------------------------
def checksum_ok(payload: str, claimed_hex: str) -> bool:
    """對 payload 逐字元 XOR，比對是否等於封包宣稱的 2 位十六進位 checksum。"""
    cs = 0
    for ch in payload:
        cs ^= ord(ch)
    try:
        claimed = int(claimed_hex, 16)
    except ValueError:
        return False
    return cs == claimed


def parse_packet(line: str, timestamp: Optional[float] = None) -> Optional[UartPacket]:
    """解析一行 '$SDK,...*CS'（前後空白會被清掉）。

    格式錯誤或 checksum 不合法回傳 None（並記 debug log），不拋例外——UART
    線路偶爾出現雜訊/斷位元組是正常現象，不該讓整個接收執行緒掛掉。
    """
    line = line.strip()
    if not line:
        return None

    if not line.startswith("$") or "*" not in line:
        logger.debug("略過非封包內容: %r", line)
        return None

    body, _, claimed_hex = line[1:].partition("*")
    if not checksum_ok(body, claimed_hex):
        logger.warning("checksum 不符，捨棄此封包: %r", line)
        return None

    fields = body.split(",")
    if len(fields) != 7 or fields[0] != "SDK":
        logger.warning("欄位數量或 ID 不符，捨棄此封包: %r", line)
        return None

    try:
        return UartPacket(
            dx=int(fields[1]),
            dy=int(fields[2]),
            yaw_deg=float(fields[3]),
            pitch_deg=float(fields[4]),
            roll_deg=float(fields[5]),
            hx711_raw=int(fields[6]),
            timestamp=timestamp if timestamp is not None else time.time(),
        )
    except ValueError:
        logger.warning("欄位型別轉換失敗，捨棄此封包: %r", line)
        return None


# ----------------------------------------------------------------------
# CSV logger（可選開關，Phase 6 異常偵測訓練資料用）
# ----------------------------------------------------------------------
class _CsvLogger:
    def __init__(self, output_dir: str | Path, filename_pattern: str, flush_every_n_rows: int = 20):
        self._dir = Path(output_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        filename = time.strftime(filename_pattern)
        self._path = self._dir / filename
        self._file = open(self._path, "a", newline="")
        self._writer = csv.writer(self._file)
        if self._file.tell() == 0:
            self._writer.writerow(
                ["timestamp", "dx", "dy", "yaw_deg", "pitch_deg", "roll_deg", "hx711_raw"]
            )
        self._flush_every = flush_every_n_rows
        self._rows_since_flush = 0
        logger.info("CSV logger 已啟用，寫入 %s", self._path)

    def write(self, packet: UartPacket) -> None:
        self._writer.writerow(packet.to_csv_row())
        self._rows_since_flush += 1
        if self._rows_since_flush >= self._flush_every:
            self._file.flush()
            self._rows_since_flush = 0

    def close(self) -> None:
        self._file.flush()
        self._file.close()


# ----------------------------------------------------------------------
# 背景執行緒接收器
# ----------------------------------------------------------------------
class UartReceiver:
    """在背景執行緒持續讀取序列埠、驗證 checksum，並把合法封包推進
    Thread-safe Queue。斷線時會依 reconnect_backoff_sec 自動重試連線，
    不會讓整個系統因為 UART 暫時異常就崩潰。
    """

    def __init__(
        self,
        port: str = "/dev/ttyAMA0",
        baudrate: int = 115200,
        timeout_sec: float = 1.0,
        reconnect_backoff_sec: float = 2.0,
        stale_data_timeout_sec: float = 0.5,
        out_queue: Optional["queue.Queue[UartPacket]"] = None,
        queue_maxsize: int = 200,
        csv_logger_config: Optional[dict] = None,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout_sec = timeout_sec
        self.reconnect_backoff_sec = reconnect_backoff_sec
        self.stale_data_timeout_sec = stale_data_timeout_sec

        self.out_queue: "queue.Queue[UartPacket]" = out_queue or queue.Queue(maxsize=queue_maxsize)

        self._csv_logger_config = csv_logger_config or {}
        self._csv_logger: Optional[_CsvLogger] = None

        self._serial: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._last_packet_time: Optional[float] = None
        self._last_packet_lock = threading.Lock()

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("UartReceiver 已經在執行中")

        if self._csv_logger_config.get("enabled"):
            self._csv_logger = _CsvLogger(
                output_dir=self._csv_logger_config.get("output_dir", "logs/uart_raw"),
                filename_pattern=self._csv_logger_config.get(
                    "filename_pattern", "uart_%Y%m%d_%H%M%S.csv"
                ),
                flush_every_n_rows=self._csv_logger_config.get("flush_every_n_rows", 20),
            )

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="UartReceiverThread", daemon=True)
        self._thread.start()
        logger.info("UART 接收器背景執行緒已啟動 (port=%s, baud=%d)", self.port, self.baudrate)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._serial is not None:
            self._serial.close()
            self._serial = None
        if self._csv_logger is not None:
            self._csv_logger.close()
            self._csv_logger = None
        logger.info("UART 接收器背景執行緒已停止")

    def is_stale(self) -> bool:
        """距離上一筆合法封包是否已經超過 stale_data_timeout_sec。

        MCU 端每輪迴圈固定送包（非事件觸發），正常情況下不該有間隙；
        state_machine.py 可以用這個方法判斷 UART 連線是否異常，
        觸發對應的錯誤狀態或 UI 警示。
        """
        with self._last_packet_lock:
            if self._last_packet_time is None:
                return True
            return (time.time() - self._last_packet_time) > self.stale_data_timeout_sec

    # ------------------------------------------------------------------
    def _open_serial(self) -> bool:
        try:
            self._serial = serial.Serial(self.port, self.baudrate, timeout=self.timeout_sec)
            logger.info("已開啟序列埠 %s", self.port)
            return True
        except serial.SerialException as exc:
            logger.error(
                "無法開啟序列埠 %s：%s。"
                "請確認接線、config.json 裡的 port 是否為實測過的裝置節點"
                "（不要用 /dev/serial0），以及沒有其他程式（getty/藍牙）占用該埠。",
                self.port, exc,
            )
            return False

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._serial is None or not self._serial.is_open:
                if not self._open_serial():
                    time.sleep(self.reconnect_backoff_sec)
                    continue

            try:
                raw = self._serial.readline()
            except serial.SerialException as exc:
                logger.error("讀取序列埠時發生錯誤，將嘗試重新連線：%s", exc)
                self._safe_close_serial()
                time.sleep(self.reconnect_backoff_sec)
                continue

            if not raw:
                continue  # timeout 沒資料，正常情況，繼續等

            try:
                line = raw.decode("ascii", errors="replace")
            except Exception:  # noqa: BLE001
                continue

            now = time.time()
            packet = parse_packet(line, timestamp=now)
            if packet is None:
                continue

            with self._last_packet_lock:
                self._last_packet_time = now

            self._push_to_queue(packet)

            if self._csv_logger is not None:
                self._csv_logger.write(packet)

    def _push_to_queue(self, packet: UartPacket) -> None:
        try:
            self.out_queue.put_nowait(packet)
        except queue.Full:
            # 消費者跟不上時，丟掉最舊的一筆，保留最新資料（定位/秤重比對通常
            # 更在意「現在」的狀態，堆積舊資料反而會讓下游延遲累積）。
            try:
                self.out_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.out_queue.put_nowait(packet)
            except queue.Full:
                logger.warning("輸出佇列持續滿載，捨棄一筆封包")

    def _safe_close_serial(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001
                pass
            self._serial = None


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cfg = _load_config()
    serial_cfg = cfg.get("serial", {})
    csv_cfg = cfg.get("csv_logger", {})

    parser = argparse.ArgumentParser(description="UART 接收器獨立測試工具")
    parser.add_argument("--port", default=serial_cfg.get("port", "/dev/ttyAMA0"))
    parser.add_argument("--baud", type=int, default=serial_cfg.get("baudrate", 115200))
    parser.add_argument("--csv-log", action="store_true", help="強制啟用 CSV logger（覆蓋 config.json 設定）")
    args = parser.parse_args()

    if args.csv_log:
        csv_cfg = {**csv_cfg, "enabled": True}

    receiver = UartReceiver(
        port=args.port,
        baudrate=args.baud,
        timeout_sec=serial_cfg.get("timeout_sec", 1.0),
        reconnect_backoff_sec=serial_cfg.get("reconnect_backoff_sec", 2.0),
        stale_data_timeout_sec=cfg.get("uart_protocol", {}).get("stale_data_timeout_sec", 0.5),
        csv_logger_config=csv_cfg,
    )
    receiver.start()
    print(f"監聽 {args.port} @ {args.baud} 8N1 中...（Ctrl+C 結束）")

    try:
        while True:
            try:
                packet = receiver.out_queue.get(timeout=1.0)
            except queue.Empty:
                if receiver.is_stale():
                    print("[warn] 已超過 stale timeout 沒收到新封包", file=sys.stderr)
                continue
            print(
                f"dx={packet.dx:+d} dy={packet.dy:+d}  "
                f"yaw={packet.yaw_deg:+6.1f} pitch={packet.pitch_deg:+6.1f} "
                f"roll={packet.roll_deg:+6.1f}  hx711={packet.hx711_raw}"
            )
    except KeyboardInterrupt:
        print("\n結束中...")
    finally:
        receiver.stop()


if __name__ == "__main__":
    _main()
