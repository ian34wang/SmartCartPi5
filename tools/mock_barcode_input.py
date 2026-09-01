"""
tools/mock_barcode_input.py

沒有實體 USB 條碼掃描器時，用 Linux uinput 建立一個虛擬鍵盤裝置，模擬掃描器
「打字 + Enter」的行為，讓 drivers/barcode_scanner.py（真正的 evdev 讀取邏輯）
可以在不接硬體的情況下被測試。

這比直接呼叫 BarcodeScanner 的內部方法更貼近真實情況：它是從 /dev/input
這一層模擬，跟真實掃描器的事件路徑完全一樣，drivers/barcode_scanner.py
不需要為了測試改任何程式碼。

需要權限：建立 uinput 裝置通常需要 root，或是使用者要有 /dev/uinput 的讀寫權限
（sudo usermod -aG input $USER 通常不夠，uinput 群組要另外設定 udev rule）。

用法：
    sudo python3 -m tools.mock_barcode_input 4710018001234
    sudo python3 -m tools.mock_barcode_input 4710018001234 --repeat 5 --interval 2
"""

from __future__ import annotations

import argparse
import logging
import time

try:
    from evdev import UInput, ecodes as e
except ImportError:  # pragma: no cover
    UInput = None
    e = None

logger = logging.getLogger(__name__)

_CHAR_TO_KEYCODE = {
    "0": "KEY_0", "1": "KEY_1", "2": "KEY_2", "3": "KEY_3", "4": "KEY_4",
    "5": "KEY_5", "6": "KEY_6", "7": "KEY_7", "8": "KEY_8", "9": "KEY_9",
    "-": "KEY_MINUS", ".": "KEY_DOT", "/": "KEY_SLASH",
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _CHAR_TO_KEYCODE[_c] = f"KEY_{_c.upper()}"


def _build_capabilities() -> dict:
    assert e is not None
    keys = [getattr(e, code) for code in set(_CHAR_TO_KEYCODE.values())]
    keys.append(e.KEY_ENTER)
    return {e.EV_KEY: keys}


def send_barcode(ui: "UInput", barcode: str, key_delay_sec: float = 0.01) -> None:
    assert e is not None
    for ch in barcode.lower():
        keycode_name = _CHAR_TO_KEYCODE.get(ch)
        if keycode_name is None:
            logger.warning("找不到對應鍵碼，略過字元 %r", ch)
            continue
        keycode = getattr(e, keycode_name)
        ui.write(e.EV_KEY, keycode, 1)  # key down
        ui.syn()
        ui.write(e.EV_KEY, keycode, 0)  # key up
        ui.syn()
        time.sleep(key_delay_sec)

    ui.write(e.EV_KEY, e.KEY_ENTER, 1)
    ui.syn()
    ui.write(e.EV_KEY, e.KEY_ENTER, 0)
    ui.syn()


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if UInput is None:
        raise SystemExit(
            "evdev 套件未安裝或此平台不支援虛擬輸入裝置（僅支援 Linux）。"
            "請在 Pi 5 上執行：pip install evdev，並以 root 或有 /dev/uinput 權限的使用者執行本工具。"
        )

    parser = argparse.ArgumentParser(description="模擬 USB 條碼掃描器鍵盤輸入（CI / 無硬體測試用）")
    parser.add_argument("barcode", help="要模擬掃描的條碼字串（只支援數字/小寫英文/-./）")
    parser.add_argument("--repeat", type=int, default=1, help="重複掃描次數")
    parser.add_argument("--interval", type=float, default=1.0, help="每次掃描之間的間隔秒數")
    args = parser.parse_args()

    ui = UInput(_build_capabilities(), name="SmartCart-MockBarcodeScanner")
    logger.info("虛擬條碼掃描器裝置已建立，準備送出條碼：%s", args.barcode)
    time.sleep(0.5)  # 讓其他程式（例如 barcode_scanner.py 的裝置探索）有時間看到新裝置

    try:
        for i in range(args.repeat):
            send_barcode(ui, args.barcode)
            logger.info("已送出第 %d/%d 次掃描", i + 1, args.repeat)
            if i < args.repeat - 1:
                time.sleep(args.interval)
    finally:
        ui.close()


if __name__ == "__main__":
    _main()
