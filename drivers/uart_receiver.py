"""
drivers/uart_receiver.py

MCU（SAM-IoT WG，SAMD21）的 UART 遙測封包接收器：背景執行緒讀序列埠、驗 checksum，把合法封包放進
queue.Queue；消費端（app 的 UART 執行緒、校正工具）從佇列取。佇列滿了丟最舊的，保留最新的。
斷線自動重連。

封包格式 v2（MCU 20 Hz 固定送出，沒有新資料就重送上一次的值）：
    $SDK,<dx>,<dy>,<squal>,<yaw_deg>,<pitch_deg>,<roll_deg>,<hx711_raw>*<CS>\\r\\n

    dx, dy          int    PMW3901 這一輪的相對位移（count，不是累積值）
    squal           int    PMW3901 追蹤品質 0~255，越高越可信
    yaw/pitch/roll  float  BNO080 姿態角（度，1 位小數）；yaw 是相對航向，開機為 0
    hx711_raw       int    HX711 原始 ADC 值（換算成公克在 Pi 端做，見 core/shopping/weight.py）
    CS              hex    2 位十六進位，$ 與 * 之間所有字元的 XOR（NMEA 算法）

序列埠用實測過的 /dev/ttyAMA0（config.json 的 serial.port）。Pi 5 的 /dev/serial0 可能指到
別的 UART 控制器，不要用。

硬體測試（看 MCU 有沒有正常送資料）：
    python3 -m drivers.uart_receiver                    # 印每一筆封包，每秒一行統計
    python3 -m drivers.uart_receiver --port /dev/ttyAMA0 --quiet   # 只印統計
"""

from __future__ import annotations

import argparse
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import serial

logger = logging.getLogger(__name__)


@dataclass
class UartPacket:
    dx: int
    dy: int
    squal: int
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    hx711_raw: int
    timestamp: float  # 收到的本機時間（time.time()）


def checksum_ok(payload: str, claimed_hex: str) -> bool:
    """payload 逐字元 XOR，比對封包宣稱的 2 位十六進位 checksum。"""
    cs = 0
    for ch in payload:
        cs ^= ord(ch)
    try:
        return cs == int(claimed_hex, 16)
    except ValueError:
        return False


def parse_packet(line: str, timestamp: Optional[float] = None) -> Optional[UartPacket]:
    """解析一行 '$SDK,...*CS'。格式錯或 checksum 不對回 None（不丟例外：線路偶爾有雜訊是正常的）。"""
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
    if len(fields) != 8 or fields[0] != "SDK":
        logger.warning("欄位數不對（收到 %d 欄，v2 格式是 8 欄、含 squal；每包都這樣就是 MCU 還在送舊版 7 欄）: %r",
                       len(fields), line)
        return None
    try:
        return UartPacket(
            dx=int(fields[1]), dy=int(fields[2]), squal=int(fields[3]),
            yaw_deg=float(fields[4]), pitch_deg=float(fields[5]), roll_deg=float(fields[6]),
            hx711_raw=int(fields[7]),
            timestamp=timestamp if timestamp is not None else time.time(),
        )
    except ValueError:
        logger.warning("欄位型別轉換失敗，捨棄此封包: %r", line)
        return None


class UartReceiver:
    def __init__(
        self,
        port: str = "/dev/ttyAMA0",
        baudrate: int = 115200,
        timeout_sec: float = 1.0,
        reconnect_backoff_sec: float = 2.0,
        stale_data_timeout_sec: float = 0.5,
        queue_maxsize: int = 200,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout_sec = timeout_sec
        self.reconnect_backoff_sec = reconnect_backoff_sec
        self.stale_data_timeout_sec = stale_data_timeout_sec
        self.out_queue: "queue.Queue[UartPacket]" = queue.Queue(maxsize=queue_maxsize)
        self.bad_lines = 0      # 讀到但解析失敗的行數（硬體測試的統計用）

        self._serial: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_packet_time: Optional[float] = None
        self._last_packet_lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: dict, port: Optional[str] = None, baudrate: Optional[int] = None) -> "UartReceiver":
        """用 config.json 的 serial、uart_protocol 區塊建立；port、baudrate 可以覆蓋。"""
        s = cfg.get("serial", {})
        return cls(
            port=port or s.get("port", "/dev/ttyAMA0"),
            baudrate=baudrate or int(s.get("baudrate", 115200)),
            timeout_sec=float(s.get("timeout_sec", 1.0)),
            reconnect_backoff_sec=float(s.get("reconnect_backoff_sec", 2.0)),
            stale_data_timeout_sec=float(cfg.get("uart_protocol", {}).get("stale_data_timeout_sec", 0.5)),
        )

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("UartReceiver 已經在執行中")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="UartReceiverThread", daemon=True)
        self._thread.start()
        logger.info("UART 接收器已啟動 (port=%s, baud=%d)", self.port, self.baudrate)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._safe_close_serial()
        logger.info("UART 接收器已停止")

    def is_stale(self) -> bool:
        """距離上一筆合法封包是否超過 stale_data_timeout_sec（MCU 固定 20 Hz 送，正常不該有間隙）。"""
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
            logger.error("無法開啟序列埠 %s：%s（確認接線、port 是實測過的節點而不是 /dev/serial0、"
                         "沒有 getty 或其他程式佔用）", self.port, exc)
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
                logger.error("讀取序列埠錯誤，重新連線：%s", exc)
                self._safe_close_serial()
                time.sleep(self.reconnect_backoff_sec)
                continue
            if not raw:
                continue  # 逾時沒資料

            now = time.time()
            packet = parse_packet(raw.decode("ascii", errors="replace"), timestamp=now)
            if packet is None:
                self.bad_lines += 1
                continue
            with self._last_packet_lock:
                self._last_packet_time = now
            self._push(packet)

    def _push(self, packet: UartPacket) -> None:
        try:
            self.out_queue.put_nowait(packet)
        except queue.Full:
            # 消費端跟不上：丟最舊的一筆，保留最新的（定位、秤重在意的是現在）
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


# ----------------------------------------------------------------------
# 硬體測試
# ----------------------------------------------------------------------
def _main() -> int:
    from core import config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cfg = config.load()
    ap = argparse.ArgumentParser(description="UART 硬體測試：確認 MCU 有在送合法封包")
    ap.add_argument("--port", default=None, help="覆蓋 config.json 的 serial.port")
    ap.add_argument("--baud", type=int, default=None, help="覆蓋 config.json 的 serial.baudrate")
    ap.add_argument("--quiet", action="store_true", help="不印每一筆，只印每秒統計")
    args = ap.parse_args()

    rx = UartReceiver.from_config(cfg, port=args.port, baudrate=args.baud)
    rx.start()
    print(f"監聽 {rx.port} @ {rx.baudrate}（Ctrl+C 結束）")
    n, bad0, t0 = 0, 0, time.time()
    try:
        while True:
            try:
                p = rx.out_queue.get(timeout=0.2)
                n += 1
                if not args.quiet:
                    print(f"dx={p.dx:+4d} dy={p.dy:+4d} squal={p.squal:3d}  yaw={p.yaw_deg:+6.1f} "
                          f"pitch={p.pitch_deg:+6.1f} roll={p.roll_deg:+6.1f}  hx711={p.hx711_raw}")
            except queue.Empty:
                pass
            if time.time() - t0 >= 1.0:
                bad = rx.bad_lines - bad0
                state = "沒有資料" if n == 0 else ("正常" if n >= 15 and bad == 0 else "注意")
                print(f"[統計] {n} 包/秒、壞封包 {bad}　{state}")
                n, bad0, t0 = 0, rx.bad_lines, time.time()
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
