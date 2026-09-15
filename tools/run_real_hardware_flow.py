"""
tools/run_real_hardware_flow.py

純終端機的實機整合測試：把三個真實硬體同時接上 `CartStateMachine`，在真的推車
上跑完整流程，不需要觸控 UI。跟 `ui/app_gui.py` 是同一套硬體、同一套狀態機，
差別只在這支沒有畫面、而且會順便印出 Phase 3 的定位座標——站在推車旁邊一邊推
一邊看數字時比較方便。

    UART        BNO080 yaw + PMW3901 光流 + HX711 秤重（drivers/uart_receiver.py）
    條碼掃描器  USB HID，evdev 獨佔（drivers/barcode_scanner.py）
    管制區閘門  BLE 雙 Beacon 差分（drivers/ble_beacon_scanner.py + core/gate_monitor.py）

**三個硬體都是硬性條件，任何一個接不上就直接結束，沒有模擬或退回路徑。**
之前這支有 `--no-scanner`、鍵盤模擬閘門進出等旁路，結果是出問題時要先搞清楚
自己走在哪一條路上，而且畫面/終端機會在硬體其實沒在運作的情況下看起來正常。

還是用鍵盤輸入的，只有「本來就該由人操作」的那幾個動作——鎖定結帳、確認付款、
登出、工作人員強制登出、秤重異常時重試/放棄。這些在正式產品裡是觸控螢幕上的
按鈕（見 ui/app_gui.py），不是硬體感測器，所以在這支純終端機工具裡用鍵盤代替
是它原本的介面，不是旁路。

用法：
    python3 -m tools.run_real_hardware_flow
    python3 -m tools.run_real_hardware_flow --port /dev/ttyAMA0 --barcode-hint USBKey

執行中輸入以下單一字元指令（Enter 送出）：
    l = 鎖定結帳                p = 確認付款完成
    o = 登出                    f = 強制登出（工作人員）
    r = 秤重異常時重試比對        v = 秤重異常時放棄這筆商品
    s = 印出目前完整狀態          q = 結束

登入/商品掃碼直接刷條碼；進出管制區直接推車通過門口（BLE 自動判定）；秤重比對
背景自動用真實 HX711 數據跑。加入/移除不用切換模式，狀態機會自己依「掃碼跟重量
變化的先後順序」判斷。
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional

from core.cart_manager import CartManager
from core.cart_state_machine import (
    CartStateMachine,
    GateEntryDetected,
    GateExitDetected,
    ItemScanned,
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
from core.gate_monitor import GATE_CROSSING_ENTERING, GateMonitor
from core.landmark_correction import load_landmark_config
from drivers.barcode_scanner import BarcodeEvent, BarcodeScanner
from drivers.ble_beacon_scanner import (
    BleBeaconScanner,
    load_beacon_identity_map,
    load_beacon_ids,
)
from drivers.uart_receiver import UartPacket, UartReceiver

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


# ----------------------------------------------------------------------
# 純邏輯部分（可離線單元測試，不需要真的硬體）
# ----------------------------------------------------------------------
def classify_barcode(code: str, login_prefix: str) -> str:
    """把一次掃描結果分類成「登入」還是「商品掃碼」，回傳 "login" 或 "item"。

    加入/移除不在這裡判斷——那是 `CartStateMachine` 自己看「掃碼跟重量變化
    的先後順序」自動推斷的（見檔案開頭說明），這支函式只負責分類條碼本身
    是登入條碼還是商品條碼。純函式，不碰狀態機或硬體，方便單獨測試。
    """
    if login_prefix and code.startswith(login_prefix):
        return "login"
    return "item"


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

    parser = argparse.ArgumentParser(
        description="實機整合流程測試（真實 UART + 條碼掃描器 + BLE 閘門，任一接不上就不啟動）"
    )
    parser.add_argument("--port", default=default_port)
    parser.add_argument("--baud", type=int, default=default_baud)
    parser.add_argument("--px-to-mm", type=float, default=default_px_to_mm)
    parser.add_argument("--min-squal", type=int, default=0)
    parser.add_argument("--barcode-hint", default=default_barcode_hint)
    parser.add_argument("--db", default=None)
    parser.add_argument("--adapter", default=None, help="藍牙介面名稱，預設用系統預設（通常 hci0）")
    parser.add_argument("--beacon-timeout", type=float, default=20.0,
                        help="開機時等門口 Beacon 出現的秒數，逾時就不啟動（預設 20）")
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

    landmark_cfg = load_landmark_config()
    try:
        gate_monitor = GateMonitor.from_config(landmark_cfg)
    except ValueError as exc:
        print(f"[錯誤] 門口 Beacon 設定不完整：{exc}")
        return 1

    uart = UartReceiver(port=args.port, baudrate=args.baud)
    scanner = None
    ble = None
    stop_event = threading.Event()

    def _cleanup() -> None:
        stop_event.set()
        for dev in (uart, scanner, ble):
            if dev is not None:
                try:
                    dev.stop()
                except Exception:  # noqa: BLE001
                    pass

    try:
        scanner = BarcodeScanner(device_name_hint=args.barcode_hint)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[錯誤] 條碼掃描器連不上：{exc}")
        print("  1) pip install evdev　2) sudo usermod -aG input $USER（重新登入生效）")
        print("  3) 查裝置名稱：sudo python3 -m drivers.barcode_scanner --list\n")
        return 1

    try:
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
        print(f"BLE 門口 Beacon 都掃到了。{gate_monitor.describe()}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[錯誤] BLE 門口 Beacon 連不上：{exc}")
        print("  1) sudo rfkill unblock bluetooth　2) pip install bleak")
        print("  3) 查實際位址/名稱：python3 -m drivers.ble_beacon_scanner --list\n")
        _cleanup()
        return 1

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
            gate_monitor.update_heading(packet.yaw_deg)
            grams = raw_to_grams(packet.hx711_raw, weight_offset, weight_scale)
            sm.process_event(WeightSampleReceived(grams=grams, timestamp=packet.timestamp))

    def barcode_consumer() -> None:
        while not stop_event.is_set():
            try:
                evt: BarcodeEvent = scanner.out_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            kind = classify_barcode(evt.code, login_prefix)
            if kind == "login":
                sm.process_event(LoginScanned(member_id=evt.code, timestamp=evt.timestamp))
            else:
                sm.process_event(ItemScanned(barcode=evt.code, timestamp=evt.timestamp))
            _print_status(sm, odometry)

    def gate_consumer() -> None:
        while not stop_event.is_set():
            try:
                obs = ble.out_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            crossing = gate_monitor.process_observation(obs.beacon_id, obs.rssi, obs.timestamp)
            if crossing is None:
                continue
            now = time.time()
            print(f"\n[門口] BLE 判定：{crossing}（{obs.beacon_id} RSSI={obs.rssi}）")
            if crossing == GATE_CROSSING_ENTERING:
                sm.process_event(GateEntryDetected(timestamp=now))
            else:
                sm.process_event(GateExitDetected(timestamp=now))
            _print_status(sm, odometry)

    def timeout_ticker() -> None:
        while not stop_event.is_set():
            time.sleep(1.0)
            sm.process_event(TimeoutTick(now=time.time()))

    def command_reader() -> None:
        while not stop_event.is_set():
            try:
                cmd = input().strip().lower()
            except EOFError:
                break
            now = time.time()
            if cmd == "q":
                stop_event.set()
                break
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

    for t in (
        threading.Thread(target=uart_consumer, name="UartConsumer", daemon=True),
        threading.Thread(target=barcode_consumer, name="BarcodeConsumer", daemon=True),
        threading.Thread(target=gate_consumer, name="GateConsumer", daemon=True),
        threading.Thread(target=timeout_ticker, name="TimeoutTicker", daemon=True),
    ):
        t.start()

    print(f"三個硬體都就緒：UART {args.port} @ {args.baud}、"
          f"條碼掃描器（關鍵字 {args.barcode_hint!r}）、BLE 門口 Beacon。")
    _print_status(sm, odometry)

    try:
        command_reader()
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup()

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
