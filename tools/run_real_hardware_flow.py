"""
tools/run_real_hardware_flow.py

從 Phase 3 之後就沒有在實機上跑過整條流程了——這支就是用來補這件事的：把
目前「有真實硬體」的部分（UART：BNO080 yaw + PMW3901 光流 + HX711 秤重、
USB 條碼掃描器）跟「還沒有硬體」的部分（管制區閘門、鎖定/付款/登出這些目
前只能靠 UI 按鈕觸發、UI 本身要 Phase 5 才會做）接起來，讓你可以在真的推
車上測試「掃碼->秤重比對->…」這條路走不走得順，不用等閘門硬體或觸控 UI
做出來才能測。

具體接法：
    - `drivers.uart_receiver.UartReceiver`（真的 UART port）收到的每一筆
      封包，同時餵給 `core.odometry_engine.OdometryEngine`（更新位置，讓你
      也能順便看 Phase 3 dead-reckoning 現在準不準）跟換算成公克數餵給
      `core.cart_state_machine.CartStateMachine` 的 `WeightSampleReceived`
      （這是秤重比對邏輯第一次真的接上實機數據，之前只有 `--simulate` 用
      假數字測過）。
    - `drivers.barcode_scanner.BarcodeScanner`（真的 USB 掃描器）掃到的每
      一組條碼，依 `config.json` 的 `state_machine.login_barcode_prefix`
      自動判斷是「登入」還是「商品掃碼」，餵進狀態機——這也是第一次真的用
      實體掃描器觸發狀態機，不是打字模擬。
    - 閘門進出、鎖定結帳、付款完成、登出這幾個目前沒有硬體/UI 來源的事件，
      用終端機打字模擬（跟 `core.cart_state_machine --simulate` 同一套指
      令，只是跟真實資料流同時跑），輸入單一字元就好，不用打整行指令，這
      樣手在推車旁邊操作時比較方便。

用法：
    python3 -m tools.run_real_hardware_flow
    python3 -m tools.run_real_hardware_flow --port /dev/ttyAMA0 --barcode-hint USBKey

執行中輸入以下單一字元指令（Enter 送出）：
    e = 模擬進入管制區          x = 模擬走出管制區
    m = 切換掃碼模式（加入/移除）  l = 鎖定結帳
    p = 確認付款完成            o = 登出
    f = 強制登出（工作人員）      s = 印出目前完整狀態
    q = 結束
真正的登入/商品掃碼直接刷條碼即可，不用打字；秤重比對是背景自動用真實
HX711 數據跑的，不用手動觸發。
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from core.cart_manager import CartManager
from core.cart_state_machine import (
    CartStateMachine,
    GateEntryDetected,
    GateExitDetected,
    ItemScanned,
    ITEM_SCAN_MODE_ADD,
    ITEM_SCAN_MODE_REMOVE,
    LockForCheckoutRequested,
    LoginScanned,
    LogoutRequested,
    ForceLogoutRequested,
    PaymentConfirmed,
    RetryWeightCheckRequested,
    VoidPendingItemRequested,
    SensorDisconnected,
    SensorReconnected,
    STATE_WEIGHT_MISMATCH_ERROR,
    TimeoutTick,
    WeightSampleReceived,
    load_state_machine_config,
)
from core.odometry_engine import OdometryEngine
from core.weight_convert import raw_to_grams
from database.db_manager import DBManager
from drivers.barcode_scanner import BarcodeEvent, BarcodeScanner
from drivers.uart_receiver import UartPacket, UartReceiver

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


# ----------------------------------------------------------------------
# 純邏輯部分（可離線單元測試，不需要真的硬體）
# ----------------------------------------------------------------------
def classify_barcode(code: str, current_mode: str, login_prefix: str) -> Tuple[str, str]:
    """把一次掃描結果分類成「登入」還是「商品掃碼」。回傳 (kind, mode_or_empty)：
    kind 是 "login" 或 "item"；是 "item" 時第二個值是目前的加入/移除模式。

    純函式，不碰狀態機或硬體，方便單獨測試分類邏輯對不對，不用真的刷卡。
    """
    if login_prefix and code.startswith(login_prefix):
        return ("login", "")
    return ("item", current_mode)


def uart_stale_timeout_sec(cfg: dict) -> float:
    return cfg.get("uart_protocol", {}).get("stale_data_timeout_sec", 0.5)


# ----------------------------------------------------------------------
def load_full_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _print_status(sm: CartStateMachine, odometry: OdometryEngine) -> None:
    s = sm.get_session()
    pos = odometry.get_state()
    print(f"\n--- 購物流程狀態：{s.state} ---")
    print(f"會員：{s.member_id}　目前重量：{s.current_weight_g}　感測器連線：{s.sensor_connected}")
    if s.pending_item_barcode:
        print(
            f"等待比對中：條碼={s.pending_item_barcode} 模式={s.pending_item_mode} "
            f"預期變化={s.pending_item_expected_delta_g:+.1f}±{s.pending_item_tolerance_g:.1f}g"
        )
    items = sm.cart.list_items()
    if items:
        print("購物清單：")
        for it in items:
            print(f"  {it.name} x{it.quantity}　${it.subtotal:.0f}")
        print(f"總計：${sm.cart.total_price():.0f}")
    if s.recent_alerts:
        print("最近警告：")
        for a in s.recent_alerts[-3:]:
            print(f"  [{a.severity}] {a.code}: {a.message}")
    print(
        f"--- Phase 3 定位（順便看一下）：X={pos.x_mm:.0f}mm Y={pos.y_mm:.0f}mm "
        f"yaw={pos.yaw_deg:+.1f} 樣本數={pos.sample_count} 低信心跳過={pos.skipped_low_confidence_count} ---"
    )
    if s.state == STATE_WEIGHT_MISMATCH_ERROR:
        print("（目前秤重異常：於指令列輸入 r 重試比對，或 v 放棄這筆商品）")


# ----------------------------------------------------------------------
def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    cfg = load_full_config()
    default_port = cfg.get("serial", {}).get("port", "/dev/ttyAMA0")
    default_baud = cfg.get("serial", {}).get("baudrate", 115200)
    default_px_to_mm = cfg.get("odometry", {}).get("optical_flow_px_to_mm", 1.0)
    default_barcode_hint = cfg.get("barcode_scanner", {}).get("device_name_hint", "Barcode")
    weight_offset = cfg.get("weight", {}).get("hx711_offset", 0)
    weight_scale = cfg.get("weight", {}).get("hx711_scale", 1.0)
    stale_timeout = uart_stale_timeout_sec(cfg)

    parser = argparse.ArgumentParser(description="Phase 3+4 實機整合流程測試（真實 UART+條碼掃描器，手動模擬閘門/結帳事件）")
    parser.add_argument("--port", default=default_port)
    parser.add_argument("--baud", type=int, default=default_baud)
    parser.add_argument("--px-to-mm", type=float, default=default_px_to_mm)
    parser.add_argument("--min-squal", type=int, default=0)
    parser.add_argument("--barcode-hint", default=default_barcode_hint)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    if args.px_to_mm == 1.0:
        print("[警告] optical_flow_px_to_mm 還是 CALIBRATE_ME 佔位值，位置只能看趨勢，不是真實公釐數。")
    if weight_scale == 1.0:
        print("[警告] weight.hx711_scale 還是 CALIBRATE_ME 佔位值，秤重比對用的是未校正的數字，只能看流程對不對，數字本身不準。")

    db = DBManager(args.db)
    db.init_db(seed=True)
    sm_cfg = load_state_machine_config()
    sm = CartStateMachine(db=db, cart=CartManager(), config=sm_cfg)
    login_prefix = sm_cfg.get("login_barcode_prefix", "")

    odometry = OdometryEngine(px_to_mm=args.px_to_mm, min_squal=args.min_squal)

    uart = UartReceiver(port=args.port, baudrate=args.baud)
    try:
        scanner = BarcodeScanner(device_name_hint=args.barcode_hint)
    except RuntimeError as exc:
        print(f"[錯誤] 條碼掃描器無法啟動：{exc}")
        print("（如果只是想先測秤重/定位那段，可以先註解掉 scanner 相關部分——但正常應該接得到）")
        return 1

    current_mode = ITEM_SCAN_MODE_ADD
    stop_event = threading.Event()

    # ------------------------------------------------------------------
    def uart_consumer() -> None:
        last_packet_time = time.time()
        sensor_marked_disconnected = False
        while not stop_event.is_set():
            try:
                packet: UartPacket = uart.out_queue.get(timeout=0.2)
            except queue.Empty:
                if time.time() - last_packet_time > stale_timeout and not sensor_marked_disconnected:
                    sm.process_event(SensorDisconnected(timestamp=time.time()))
                    sensor_marked_disconnected = True
                continue
            last_packet_time = time.time()
            if sensor_marked_disconnected:
                sm.process_event(SensorReconnected(timestamp=packet.timestamp))
                sensor_marked_disconnected = False
            odometry.process_packet(packet)
            grams = raw_to_grams(packet.hx711_raw, weight_offset, weight_scale)
            sm.process_event(WeightSampleReceived(grams=grams, timestamp=packet.timestamp))

    def barcode_consumer() -> None:
        nonlocal current_mode
        while not stop_event.is_set():
            try:
                evt: BarcodeEvent = scanner.out_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            kind, mode = classify_barcode(evt.code, current_mode, login_prefix)
            if kind == "login":
                sm.process_event(LoginScanned(member_id=evt.code, timestamp=evt.timestamp))
            else:
                sm.process_event(ItemScanned(barcode=evt.code, mode=mode, timestamp=evt.timestamp))
            _print_status(sm, odometry)

    def timeout_ticker() -> None:
        while not stop_event.is_set():
            time.sleep(1.0)
            sm.process_event(TimeoutTick(now=time.time()))

    def command_reader() -> None:
        nonlocal current_mode
        while not stop_event.is_set():
            try:
                cmd = input().strip().lower()
            except EOFError:
                break
            now = time.time()
            if cmd == "q":
                stop_event.set()
                break
            elif cmd == "e":
                sm.process_event(GateEntryDetected(timestamp=now))
            elif cmd == "x":
                sm.process_event(GateExitDetected(timestamp=now))
            elif cmd == "m":
                current_mode = ITEM_SCAN_MODE_REMOVE if current_mode == ITEM_SCAN_MODE_ADD else ITEM_SCAN_MODE_ADD
                print(f"目前掃碼模式：{current_mode}")
            elif cmd == "l":
                sm.process_event(LockForCheckoutRequested(timestamp=now))
            elif cmd == "p":
                sm.process_event(PaymentConfirmed(timestamp=now))
            elif cmd == "o":
                sm.process_event(LogoutRequested(timestamp=now))
            elif cmd == "f":
                reason = input("原因：").strip()
                sm.process_event(ForceLogoutRequested(timestamp=now, reason=reason))
            elif cmd == "r":
                sm.process_event(RetryWeightCheckRequested(timestamp=now))
            elif cmd == "v":
                sm.process_event(VoidPendingItemRequested(timestamp=now))
            elif cmd == "s":
                pass
            else:
                print("不認得的指令，看檔案開頭的說明")
                continue
            _print_status(sm, odometry)

    uart.start()
    scanner.start()

    threads = [
        threading.Thread(target=uart_consumer, name="UartConsumer", daemon=True),
        threading.Thread(target=barcode_consumer, name="BarcodeConsumer", daemon=True),
        threading.Thread(target=timeout_ticker, name="TimeoutTicker", daemon=True),
    ]
    for t in threads:
        t.start()

    print(__doc__)
    print(f"監聽 UART {args.port} @ {args.baud}、條碼掃描器（關鍵字 '{args.barcode_hint}'）...")
    _print_status(sm, odometry)

    try:
        command_reader()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        uart.stop()
        scanner.stop()

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
