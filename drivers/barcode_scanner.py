"""
drivers/barcode_scanner.py

透過 evdev 直接攔截 USB HID 條碼掃描器的 EV_KEY 事件，在背景執行緒中組出完整
條碼字串後丟進 callback / Queue，避免掃描器把按鍵事件灌進終端機標準輸入
（大部分 USB 條碼掃描器對電腦來說就是一台鍵盤，會直接把字打進 focus 的欄位，
這裡改用 evdev 直接抓 /dev/input/event*，不需要畫面上有輸入焦點）。

用法（獨立測試）：
    sudo python3 -m drivers.barcode_scanner --list       # 列出可用輸入裝置
    sudo python3 -m drivers.barcode_scanner --device-hint Barcode

注意：讀取 /dev/input/event* 通常需要 root 權限，或把使用者加進 input 群組
（sudo usermod -aG input $USER，重新登入生效）。
"""

from __future__ import annotations

import argparse
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import evdev
    from evdev import ecodes
except ImportError:  # pragma: no cover - 讓沒有 evdev 的環境（例如開發機）還能 import 這個模組
    evdev = None
    ecodes = None

logger = logging.getLogger(__name__)

# US QWERTY 鍵盤掃描碼 -> 字元對照（USB 條碼掃描器多半模擬美式鍵盤佈局）。
# 同時涵蓋數字小鍵盤(KP)版本，因為不少條碼掃描器的「鍵盤模擬」模式預設是送
# 小鍵盤數字鍵而不是上排數字鍵（實測過的 USBKey 這款就是如此），兩種都收才不會
# 漏字。
_KEYCODE_TO_CHAR = {
    "KEY_0": "0", "KEY_1": "1", "KEY_2": "2", "KEY_3": "3", "KEY_4": "4",
    "KEY_5": "5", "KEY_6": "6", "KEY_7": "7", "KEY_8": "8", "KEY_9": "9",
    "KEY_KP0": "0", "KEY_KP1": "1", "KEY_KP2": "2", "KEY_KP3": "3", "KEY_KP4": "4",
    "KEY_KP5": "5", "KEY_KP6": "6", "KEY_KP7": "7", "KEY_KP8": "8", "KEY_KP9": "9",
    "KEY_A": "a", "KEY_B": "b", "KEY_C": "c", "KEY_D": "d", "KEY_E": "e",
    "KEY_F": "f", "KEY_G": "g", "KEY_H": "h", "KEY_I": "i", "KEY_J": "j",
    "KEY_K": "k", "KEY_L": "l", "KEY_M": "m", "KEY_N": "n", "KEY_O": "o",
    "KEY_P": "p", "KEY_Q": "q", "KEY_R": "r", "KEY_S": "s", "KEY_T": "t",
    "KEY_U": "u", "KEY_V": "v", "KEY_W": "w", "KEY_X": "x", "KEY_Y": "y",
    "KEY_Z": "z",
    "KEY_MINUS": "-", "KEY_DOT": ".", "KEY_SLASH": "/",
    "KEY_KPMINUS": "-", "KEY_KPDOT": ".", "KEY_KPSLASH": "/",
}

_SHIFT_KEYS = {"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"}


@dataclass
class BarcodeEvent:
    code: str
    timestamp: float


class BarcodeScanner:
    """在背景執行緒監聽 evdev 裝置，組出條碼字串後透過 callback 或 Queue 送出。

    condeg 事件流通常是：一連串很快的按鍵 down/up，最後以 ENTER 結尾。
    key_timeout_sec 是保險機制：如果掃描器不送 ENTER 或中途斷線，超過這個
    間隔沒有新按鍵就視為一次掃碼結束，避免字串卡住不送出。
    """

    def __init__(
        self,
        device_path: Optional[str] = None,
        device_name_hint: str = "Barcode",
        key_timeout_sec: float = 0.1,
        on_scan: Optional[Callable[[BarcodeEvent], None]] = None,
        out_queue: Optional["queue.Queue[BarcodeEvent]"] = None,
        debug_log_keys: bool = False,
    ):
        if evdev is None:
            raise RuntimeError(
                "evdev 套件未安裝或此平台不支援（僅支援 Linux）。"
                "請在 Pi 5 上執行：pip install evdev"
            )

        self.device_path = device_path
        self.device_name_hint = device_name_hint
        self.key_timeout_sec = key_timeout_sec
        self.on_scan = on_scan
        self.out_queue = out_queue
        self.debug_log_keys = debug_log_keys
        """debug_log_keys=True 時，每個按下的鍵都會用 INFO 等級印出實際 keycode
        （包含對照不到的），用來診斷新型號掃描器實際送出的鍵碼是什麼（例如小鍵盤
        版數字鍵、非 US 佈局等），不用先猜測要加哪些鍵碼對照。"""

        self._device: Optional["evdev.InputDevice"] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._buffer: list[str] = []
        self._shift_pressed = False
        self._capslock_led_state = 0

    # ------------------------------------------------------------------
    # 裝置探索
    # ------------------------------------------------------------------
    @staticmethod
    def list_devices() -> list["evdev.InputDevice"]:
        if evdev is None:
            raise RuntimeError("evdev 套件未安裝或此平台不支援。")
        return [evdev.InputDevice(p) for p in evdev.list_devices()]

    def _resolve_device(self) -> "evdev.InputDevice":
        if self.device_path:
            return evdev.InputDevice(self.device_path)

        candidates = self.list_devices()
        for dev in candidates:
            if self.device_name_hint.lower() in dev.name.lower():
                logger.info("找到條碼掃描器裝置：%s (%s)", dev.name, dev.path)
                return dev

        names = [f"{d.path}: {d.name}" for d in candidates]
        raise RuntimeError(
            f"找不到名稱包含 '{self.device_name_hint}' 的輸入裝置。"
            f"目前可用裝置：{names}。"
            "請用 --list 確認實際裝置名稱後，在 config.json 的 "
            "barcode_scanner.device_name_hint 填入正確關鍵字，"
            "或直接指定 device_path。"
        )

    # ------------------------------------------------------------------
    # 執行緒生命週期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("BarcodeScanner 已經在執行中")
        self._device = self._resolve_device()
        # 獨佔裝置，避免事件同時被其他程式（例如桌面環境）當成鍵盤輸入處理
        try:
            self._device.grab()
        except Exception as exc:  # noqa: BLE001
            logger.warning("無法 grab 裝置（可能已被其他程式獨佔）：%s", exc)

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="BarcodeScannerThread", daemon=True
        )
        self._thread.start()
        logger.info("條碼掃描器背景執行緒已啟動")

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._device is not None:
            try:
                self._device.ungrab()
            except Exception:  # noqa: BLE001
                pass
            self._device.close()
            self._device = None
        logger.info("條碼掃描器背景執行緒已停止")

    # ------------------------------------------------------------------
    # 主要讀取迴圈
    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        assert self._device is not None
        # python-evdev 開啟裝置時就已經用 O_NONBLOCK 開 fd，不需要（也沒有）
        # set_nonblocking() 這個方法可呼叫；用 select() 等待 fd 可讀即可。
        import select
        import time as _time

        last_key_time = _time.monotonic()

        try:
            while not self._stop_event.is_set():
                r, _, _ = select.select([self._device.fd], [], [], self.key_timeout_sec)
                now = _time.monotonic()

                if not r:
                    # 沒有新事件；若緩衝區有內容且已超時，強制結束這次掃碼
                    if self._buffer and (now - last_key_time) > self.key_timeout_sec:
                        self._flush_buffer(now)
                    continue

                for event in self._device.read():
                    if self.debug_log_keys:
                        # 印出「所有」原始事件（不只 EV_KEY），尤其是 EV_MSC/MSC_SCAN——
                        # 這是核心翻譯成 EV_KEY 之前的原始 HID scancode。如果某些掃描器
                        # 的鍵碼翻譯本身就錯亂（例如全部映射成同一個鍵），MSC_SCAN 的原始
                        # 值仍然是掃描器實際送出的東西，可以用來繞過壞掉的鍵碼翻譯。
                        type_name = ecodes.EV.get(event.type, str(event.type))
                        if event.type == ecodes.EV_MSC:
                            code_name = ecodes.MSC.get(event.code, str(event.code))
                        elif event.type == ecodes.EV_KEY:
                            code_name = ecodes.KEY.get(event.code, str(event.code))
                        else:
                            code_name = str(event.code)
                        logger.info(
                            "[debug/raw] type=%s(%d) code=%s(%d) value=%s",
                            type_name, event.type, code_name, event.code, event.value,
                        )

                    if event.type != ecodes.EV_KEY:
                        continue
                    key_event = evdev.categorize(event)
                    keycode = key_event.keycode
                    if isinstance(keycode, list):
                        keycode = keycode[0]
                    is_key_down = key_event.keystate == evdev.KeyEvent.key_down
                    last_key_time = self._process_key(keycode, is_key_down, now, last_key_time)
        except OSError as exc:
            logger.error("條碼掃描器讀取迴圈異常結束：%s", exc)

    def _process_key(self, keycode: str, is_key_down: bool, now: float, last_key_time: float) -> float:
        """處理單一按鍵事件，更新內部緩衝區/shift 狀態。

        獨立成一個不依賴實體裝置的方法，方便在沒有 uinput/實體掃描器的環境下
        直接用假的 (keycode, is_key_down) 事件序列做單元測試。回傳值是更新後的
        last_key_time（呼叫端要記得覆蓋原本的變數）。
        """
        if self.debug_log_keys:
            logger.info("[debug] keycode=%s keydown=%s", keycode, is_key_down)

        # Shift 的按下/放開都要追蹤，才能正確判斷大小寫（放開時也要處理，
        # 不能提早 return，否則 shift 放開後狀態不會清掉）。
        if keycode in _SHIFT_KEYS:
            self._shift_pressed = is_key_down
            return last_key_time

        if keycode == "KEY_CAPSLOCK":
            # 部分 HID 掃描器在送出真正的條碼字元前，會先按一次 Caps Lock，
            # 用意是「詢問」目前作業系統的大小寫 LED 狀態，並期待作業系統把 LED
            # 狀態回應（echo）回裝置，才會繼續往下送資料。
            #
            # 這個 echo 正常是由 Linux 核心自己的 kbd/leds handler 處理，但因為
            # BarcodeScanner.start() 會呼叫 device.grab() 把裝置獨佔給我們自己，
            # 核心那個 handler 就再也收不到這個裝置的事件，echo 就斷了——裝置會
            # 誤以為沒人回應，開始瘋狂重試（實測症狀：接上後一刷條碼就開始每隔
            # ~50ms 狂閃 Caps Lock，直到我們 stop()／ungrab() 之後，核心 handler
            # 重新接手才瞬間把資料送出）。這裡手動模擬核心原本會做的 LED echo，
            # 讓裝置滿意、繼續往下送真正的條碼資料。
            if is_key_down:
                self._echo_capslock_led()
            return last_key_time

        if not is_key_down:
            return last_key_time

        last_key_time = now
        if keycode in ("KEY_ENTER", "KEY_KPENTER"):
            self._flush_buffer(now)
            return last_key_time

        char = _KEYCODE_TO_CHAR.get(keycode)
        if char is not None:
            if char.isalpha():
                # 標準鍵盤語意：Shift 和 Caps Lock 都會讓字母變大寫，但兩個同時
                # 「開」會互相抵銷（等同沒開）——用 XOR 表示。這台掃描器實測會用
                # Caps Lock 開關本身來決定大小寫（不是每次都送 Shift），所以大小寫
                # 判斷必須把 _capslock_led_state 也算進去，不能只看 Shift。
                want_upper = self._shift_pressed != bool(self._capslock_led_state)
                char = char.upper() if want_upper else char.lower()
            self._buffer.append(char)
        else:
            logger.debug("忽略未對照的按鍵：%s", keycode)
        return last_key_time

    def _echo_capslock_led(self) -> None:
        """模擬 Linux 核心 kbd handler 平常會做的事：收到 Caps Lock 按下時，
        把目前的大小寫 LED 狀態寫回裝置（EV_LED）。因為我們用 grab() 獨佔了
        裝置，核心自己的 handler 看不到事件、就不會自動做這件事，需要我們自己做。
        """
        self._capslock_led_state = 1 - self._capslock_led_state
        device = getattr(self, "_device", None)
        if device is None:
            return
        try:
            device.set_led(ecodes.LED_CAPSL, self._capslock_led_state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("回應 Caps Lock LED 狀態失敗：%s", exc)

    def _flush_buffer(self, timestamp: float) -> None:  # noqa: D401 - simple flush helper
        if not self._buffer:
            return
        code = "".join(self._buffer)
        self._buffer.clear()
        event = BarcodeEvent(code=code, timestamp=timestamp)
        logger.info("掃描到條碼：%s", code)
        if self.on_scan is not None:
            self.on_scan(event)
        if self.out_queue is not None:
            self.out_queue.put(event)


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="條碼掃描器獨立測試工具")
    parser.add_argument("--list", action="store_true", help="列出所有輸入裝置後結束")
    parser.add_argument("--device-path", default=None, help="直接指定 /dev/input/eventN")
    parser.add_argument("--device-hint", default="Barcode", help="用裝置名稱關鍵字自動尋找")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="印出每個按鍵的原始 keycode（含對照不到的），用來診斷新掃描器的鍵碼對照表",
    )
    args = parser.parse_args()

    if args.list:
        for dev in BarcodeScanner.list_devices():
            print(f"{dev.path}\t{dev.name}")
        return

    def _on_scan(evt: BarcodeEvent) -> None:
        print(f"[scan] {evt.code}")

    scanner = BarcodeScanner(
        device_path=args.device_path,
        device_name_hint=args.device_hint,
        on_scan=_on_scan,
        debug_log_keys=args.debug,
    )
    scanner.start()
    print("監聽中，掃描條碼試試看（Ctrl+C 結束）...")
    try:
        while True:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        scanner.stop()


if __name__ == "__main__":
    _main()
