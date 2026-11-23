"""
ui/app_gui.py

SmartCart 觸控 app 的主程式：檢查設定、依序接上四個硬體、開 pywebview 視窗、啟動背景執行緒。
畫面在 ui/templates/index.html（tools/build_app_ui.py 從 design-cart-ui/*.dc.html 產生），
畫面與流程的橋在 ui/bridge.py，背景執行緒在 ui/workers.py。

只走真實硬體，沒有模擬或旁路：四個裝置任何一個接不上就印出原因、結束（exit 1）。之前每個裝置都有
「接不到就退回模擬」的路徑，結果出問題時要先搞清楚自己走在哪一條路上，畫面還會在硬體沒運作時
看起來一切正常。寧可開不起來。

    1 條碼掃描器  drivers/barcode_scanner.py（evdev 獨佔 /dev/input/event*）
    2 UART        drivers/uart_receiver.py（秤重、航向、光流）
    3 門口 Beacon drivers/ble_beacon_scanner.py + core/gate/monitor.py
    4 前方相機    drivers/front_camera.py（找貨頁的推車位置）

畫面右上角 DEV 是唯讀診斷面板（狀態機狀態、最近的條碼、重量、門口判定次數），沒有製造事件的按鈕。

用法：
    python3 -m ui.app_gui                     # 視窗
    python3 -m ui.app_gui --fullscreen        # 全螢幕（720x1280 直立螢幕）
    python3 -m ui.app_gui --db /path/to.db
    python3 -m ui.app_gui --port /dev/ttyAMA0 --baud 115200
    python3 -m ui.app_gui --scanner-hint USBKey      # 掃描器裝置名稱關鍵字
    python3 -m ui.app_gui --adapter hci0             # 藍牙介面
    python3 -m ui.app_gui --beacon-timeout 30        # 開機時等 Beacon 的秒數

退出：右上角 ✕、鍵盤 Esc、終端機 Ctrl+C；卡住就另開終端機 `pkill -f ui.app_gui`。
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import threading
import time
from pathlib import Path
from typing import List

# ----------------------------------------------------------------------
# WebKitGTK 繪圖設定：固定一組，不吃外面殘留的環境變數
# ----------------------------------------------------------------------
# 2026-10-29 在 Pi 5 實測（Debian 13 trixie、labwc/Wayland、DSI-1 直接接螢幕、
# Mesa 26.2.2、WebKitGTK 2.54.0），用 pywebview 開設計稿逐一切換：
#     A 合成關 + DMABUF 關  正常       B 合成關 + DMABUF 開  花屏
#     C 合成開 + DMABUF 關  正常       D 全部預設（DMABUF 開）花屏
#     E GDK_BACKEND=x11      正常       F LIBGL_ALWAYS_SOFTWARE=1 正常
# 花屏 ⇔ DMABUF 開 ∧ 硬體 GPU（V3D）∧ Wayland 原生，跟合成模式無關。
#
# 採用 C：關 DMA-BUF（網頁畫面改用共享記憶體交給 UI 行程，多一次複製，這個畫面量
# 感覺不出來），合成模式保持 WebKit 預設的開啟，網頁由 GPU 繪製。版本都是最新的也會
# 花屏，所以升級後不要拿掉。
#
# 強制設定、並清掉其他繪圖變數（不用 setdefault）：終端機裡 export 過的 WEBKIT_*、GDK_BACKEND
# 會默默留著，同一支程式在不同終端機就跑出不同的繪圖路徑。被清掉的變數啟動時會印出來。
#
# 改回 CPU 繪製（例如以後改用 VNC 看畫面又花屏）：在 _RENDER_ENV 加上
# "WEBKIT_DISABLE_COMPOSITING_MODE": "1"（＝上面的 A）。
_RENDER_ENV = {"WEBKIT_DISABLE_DMABUF_RENDERER": "1"}
# 會改變繪圖路徑、但不在 _RENDER_ENV 裡的變數一律清掉（WEBKIT_ 開頭的全部算）
_RENDER_ENV_CLEAR = ("GDK_BACKEND", "LIBGL_ALWAYS_SOFTWARE", "GALLIUM_DRIVER", "MESA_LOADER_DRIVER_OVERRIDE")


def apply_render_env(environ) -> List[str]:
    """把 environ 的繪圖相關變數設成固定的一組。回傳被清掉或改掉的「名稱=原值」。

    要在 import webview 之前呼叫（WebKit 行程啟動時才讀），本檔載入時就會套用一次。
    """
    changed = []
    for key in sorted(environ):
        if (key.startswith("WEBKIT_") or key in _RENDER_ENV_CLEAR) and key not in _RENDER_ENV:
            changed.append(f"{key}={environ[key]}")
            del environ[key]
    for key, value in _RENDER_ENV.items():
        if key in environ and environ[key] != value:
            changed.append(f"{key}={environ[key]}")
        environ[key] = value
    return changed


_RENDER_ENV_CHANGED = apply_render_env(os.environ)

from core import config  # noqa: E402  （以下都在套用繪圖設定之後才 import）
from core.gate.detector import load_landmark_config  # noqa: E402
from core.gate.monitor import GateMonitor  # noqa: E402
from core.gate.recorder import GateRecorder  # noqa: E402
from core.positioning.odometry import OdometryEngine  # noqa: E402
from core.shopping.cart import CartManager  # noqa: E402
from core.shopping.state_machine import CartStateMachine, load_state_machine_config  # noqa: E402
from core.store import layout as store_layout  # noqa: E402
from database.db_manager import DBManager  # noqa: E402
from ui import workers  # noqa: E402
from ui.bridge import Bridge, JsApi  # noqa: E402

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"
SCREEN_W = 720
SCREEN_H = 1280


def _fail(message: str) -> int:
    """硬體接不上時統一的結束方式：印出原因、結束，不要半殘地開起來。"""
    print(f"\n[錯誤] {message}\n")
    return 1


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    full_cfg = config.load()
    weight_cfg = full_cfg.get("weight", {})
    default_scanner_hint = full_cfg.get("barcode_scanner", {}).get("device_name_hint", "Barcode")
    weight_offset = weight_cfg.get("hx711_offset", 0)
    weight_scale = weight_cfg.get("hx711_scale", 1.0)

    parser = argparse.ArgumentParser(
        description="SmartCart 觸控 app（只走真實硬體，任一裝置接不上就不啟動）"
    )
    parser.add_argument("--db", default=None, help="資料庫路徑，預設 database/inventory.db")
    parser.add_argument("--fullscreen", action="store_true", help="全螢幕開啟（螢幕本身是 720x1280 時建議）")
    parser.add_argument("--scanner-hint", default=default_scanner_hint,
                        help=f"條碼掃描器裝置名稱關鍵字（預設讀 config.json，目前是 {default_scanner_hint!r}）")
    parser.add_argument("--port", default=None, help="UART 序列埠，預設讀 config.json 的 serial.port")
    parser.add_argument("--baud", type=int, default=None, help="UART baudrate，預設讀 config.json")
    parser.add_argument("--adapter", default=None, help="藍牙介面名稱，預設用系統預設（通常 hci0）")
    parser.add_argument("--beacon-timeout", type=float, default=20.0,
                        help="開機時等門口 Beacon 出現的秒數，逾時就不啟動（預設 20）")
    args = parser.parse_args()

    if not _TEMPLATE_PATH.exists():
        return _fail(f"找不到 {_TEMPLATE_PATH}。用 `python3 tools/build_app_ui.py` 產生。")

    try:
        store_layout.unit_m(store_layout.load(full_cfg.get("store")))
    except ValueError as exc:
        return _fail(f"店面平面圖設定不完整：{exc}")

    try:
        import webview
    except ImportError:
        return _fail(
            "沒有安裝 pywebview。先 `pip install pywebview`；"
            "Pi 上如果啟動時說找不到 GTK/WebKit，再補裝："
            "`sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1`"
            "（找不到 4.1 這個套件名稱的話，改試 gir1.2-webkit2-4.0）"
        )

    if weight_scale == 1.0:
        print("[警告] config.json 的 weight.hx711_scale 還是 CALIBRATE_ME 佔位值，"
              "秤重數字不準（流程可以測，數值不能信）。用 tools/calibrate_weight.py 校正。")

    db = DBManager(args.db)
    db.init_db(seed=False)
    if not db.list_products():
        print("[提醒] 商品資料庫是空的，刷任何條碼都會顯示「查無此商品」。"
              "用 `python3 -m tools.product_admin` 建檔。")
    sm = CartStateMachine(db=db, cart=CartManager(), config=load_state_machine_config(full_cfg))

    landmark_cfg = load_landmark_config(full_cfg)
    try:
        # 每一筆 Beacon 讀數與判定結果都記錄到 data/gate_logs/（core/gate/recorder.py），
        # 事後用 python3 -m tools.analyze_gate_log 分析
        gate_monitor = GateMonitor.from_config(landmark_cfg, recorder=GateRecorder.for_runtime("app"))
    except ValueError as exc:
        return _fail(f"門口 Beacon 設定不完整：{exc}")

    # ------------------------------------------------------------------
    # 四個硬體，任何一個接不上就不啟動
    # ------------------------------------------------------------------
    stop_event = threading.Event()
    scanner = None
    uart = None
    ble = None
    camera = None

    def _cleanup() -> None:
        stop_event.set()
        for dev in (scanner, uart, ble, camera):
            if dev is not None:
                try:
                    dev.stop()
                except Exception:  # noqa: BLE001
                    pass
        gate_monitor.close()

    # 1) 條碼掃描器
    try:
        from drivers.barcode_scanner import BarcodeScanner

        scanner = BarcodeScanner(device_name_hint=args.scanner_hint, out_queue=queue.Queue(),
                                 key_timeout_sec=float(full_cfg.get("barcode_scanner", {}).get("key_timeout_sec", 0.1)))
        scanner.start()
        print(f"[1/4] 條碼掃描器已連上（裝置名稱關鍵字：{args.scanner_hint!r}）")
    except Exception as exc:  # noqa: BLE001
        _cleanup()
        return _fail(
            f"條碼掃描器連不上：{exc}\n"
            "  1) 裝 evdev：pip install evdev\n"
            "  2) 給讀取 /dev/input/event* 的權限（做一次就好）：sudo usermod -aG input $USER，重新登入生效\n"
            "  3) 確認裝置名稱關鍵字：sudo python3 -m drivers.barcode_scanner --list"
        )

    # 2) UART（秤重 + 姿態）
    try:
        from drivers.uart_receiver import UartReceiver

        uart = UartReceiver.from_config(full_cfg, port=args.port, baudrate=args.baud)
        uart.start()
        deadline = time.time() + 5.0
        while time.time() < deadline and uart.out_queue.empty():
            time.sleep(0.1)
        if uart.out_queue.empty():
            raise RuntimeError(f"序列埠開起來了，但 5 秒內沒有收到任何合法封包（{uart.port}）")
        print(f"[2/4] UART 已連上並收到封包（{args.port} @ {args.baud}）")
    except Exception as exc:  # noqa: BLE001
        _cleanup()
        return _fail(
            f"UART 秤重資料流連不上：{exc}\n"
            "  1) 確認下位機有在送封包，且封包是 v2 格式（8 欄位，含 squal）\n"
            f"  2) 確認 {uart.port} 存在且有權限（通常要在 dialout 群組），或用 --port 指定\n"
            "  3) 單獨測一次：python3 -m drivers.uart_receiver --port " + str(uart.port)
        )

    # 3) BLE 門口 Beacon
    try:
        from drivers.ble_beacon_scanner import (
            BleBeaconScanner,
            load_beacon_identity_map,
            load_beacon_ids,
        )

        ble = BleBeaconScanner(
            beacon_ids=load_beacon_ids(),
            address_map=load_beacon_identity_map(),
            out_queue=queue.Queue(),
            adapter=args.adapter,
        )
        ble.start()
        ble.wait_for_beacons(
            [gate_monitor.inside_beacon_id, gate_monitor.outside_beacon_id],
            timeout_sec=args.beacon_timeout,
        )
        print(f"[3/4] 門口 Beacon 都掃到了。{gate_monitor.describe()}")
    except Exception as exc:  # noqa: BLE001
        _cleanup()
        return _fail(
            f"BLE 門口 Beacon 連不上：{exc}\n"
            "  1) 藍牙有開嗎：sudo rfkill unblock bluetooth；systemctl status bluetooth\n"
            "  2) 裝 bleak：pip install bleak\n"
            "  3) 查實際位址/名稱：python3 -m drivers.ble_beacon_scanner --list"
        )

    # 4) 前方相機（定位點標記、貨架消失點 → 找貨頁的推車位置）
    vision_cfg = config.vision(full_cfg)
    try:
        from core.store.vision import AnchorDetector
        from drivers.front_camera import FrontCamera

        camera = FrontCamera(full_cfg.get("camera", {}), proc_width=int(vision_cfg["proc_width"]))
        anchor_detector = AnchorDetector(vision_cfg["anchor_marker_id"], vision_cfg["anchor_marker_size_m"], camera.K)
        print(f"[4/4] 前方相機已開（處理 {camera.size[0]}x{camera.size[1]}，"
              f"定位點標記 #{vision_cfg['anchor_marker_id']}、邊長 {vision_cfg['anchor_marker_size_m']} m）")
    except Exception as exc:  # noqa: BLE001
        _cleanup()
        return _fail(
            f"前方相機開不起來：{exc}\n"
            "  1) picamera2 與 OpenCV：sudo apt install python3-picamera2 python3-opencv\n"
            "  2) 相機排線、rpicam-hello --list-cameras 看得到相機嗎\n"
            "  3) 校正檔：python3 -m drivers.camera_stream --calibrate（config.json 的 camera.calibration_file）"
        )

    # ------------------------------------------------------------------
    window_holder: dict = {"window": None}
    bridge = Bridge(db=db, sm=sm, window_holder=window_holder,
                    gate_side=lambda: gate_monitor.snapshot()["dominant"], store=full_cfg.get("store"),
                    vision=vision_cfg)
    # 里程計（光流 + IMU，參數是 tools/calibrate_optical_flow.py 在長廊校正寫回的）→ 找貨頁的推車位置
    odo_cfg = full_cfg.get("odometry", {})
    if odo_cfg.get("optical_flow_px_to_mm", 1.0) == 1.0:
        print("[警告] config.json 的 odometry.optical_flow_px_to_mm 還是 CALIBRATE_ME 佔位值，"
              "找貨頁的推車位置不準。用 tools/calibrate_optical_flow.py 在長廊校正。")
    odometry = OdometryEngine.from_config(odo_cfg)

    window = webview.create_window(
        "SmartCart",
        _TEMPLATE_PATH.as_uri(),
        width=SCREEN_W,
        height=SCREEN_H,
        resizable=False,
        frameless=args.fullscreen,
        fullscreen=args.fullscreen,
        js_api=JsApi(bridge),
    )
    window_holder["window"] = window

    threads = [
        workers.TimeoutTicker(bridge, stop_event),
        workers.StatePusher(bridge, stop_event),
        workers.BarcodeConsumer(bridge, scanner, stop_event),
        workers.UartConsumer(bridge, uart, gate_monitor, weight_offset, weight_scale, uart.stale_data_timeout_sec,
                             stop_event, odometry=odometry),
        workers.PosPusher(bridge, stop_event),
        workers.VisionConsumer(bridge, camera, anchor_detector, vision_cfg, stop_event),
        workers.GateConsumer(bridge, ble, gate_monitor, stop_event),
    ]

    def on_loaded():
        for t in threads:
            t.start()

    window.events.loaded += on_loaded

    # Esc 鍵跟畫面右上角 ✕ 按鈕都能正常退出；Ctrl+C 在終端機也要能乾淨結束
    # （GTK 主迴圈預設會吃掉 SIGINT）。
    signal.signal(signal.SIGINT, lambda *_: window.destroy())

    if _RENDER_ENV_CHANGED:
        print("[提醒] 忽略終端機殘留的繪圖設定（app 固定用 GPU 繪製、關 DMA-BUF）："
              + "、".join(_RENDER_ENV_CHANGED))
    print(f"\n四個硬體都就緒。視窗 {SCREEN_W}x{SCREEN_H}，右上角 DEV 診斷面板 / ✕ 退出。\n")
    webview.start()
    _cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
