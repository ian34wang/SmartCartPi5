"""
ui/workers.py

app 的背景執行緒：把硬體事件餵進 Bridge（ui/bridge.py），並把畫面變化推給前端。每條都是 daemon、
看 stop_event 結束；例外記下來但不讓執行緒掛掉。

    BarcodeConsumer   USB 條碼掃描器 → Bridge.on_hardware_barcode
    UartConsumer      UART 封包 → 重量（Bridge.on_weight_sample）、航向（GateMonitor）、里程計（Bridge.on_pose）；
                      斷流超過 stale_timeout 送 SensorDisconnected，恢復送 SensorReconnected
    GateConsumer      BLE RSSI → GateMonitor 判定穿越 → Bridge.on_gate_crossing
    VisionConsumer    前方相機 → 定位點標記（Bridge.on_anchor_seen）、貨架消失點（Bridge.on_vp_heading）
    TimeoutTicker     每秒 Bridge.on_tick
    StatePusher       Bridge 要求推送時把畫面狀態推給前端
    PosPusher         每 0.5 秒把推車位置推給找貨頁
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

from core.gate.monitor import GateMonitor
from core.positioning.odometry import OdometryEngine
from core.shopping.weight import raw_to_grams
from core.store import vision as store_vision
from ui.bridge import Bridge

logger = logging.getLogger(__name__)

POS_PUSH_SEC = 0.5     # 推車位置推給找貨頁的間隔（秒）


class TimeoutTicker(threading.Thread):
    """約每秒一次，讓逾時判斷（掃碼後等不到重量、重量變了沒掃碼、付款後沒出場…）照時間觸發。"""

    def __init__(self, bridge: Bridge, stop_event: threading.Event):
        super().__init__(name="TimeoutTicker", daemon=True)
        self.bridge = bridge
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.wait(1.0):
            try:
                self.bridge.on_tick()
            except Exception:  # noqa: BLE001 — 背景執行緒，例外要記下來
                logger.exception("TimeoutTicker 內發生未預期例外")


class StatePusher(threading.Thread):
    """硬體事件造成畫面變化時（Bridge.request_push），把最新畫面狀態推給前端。連續的變化合併成一次推送。"""

    def __init__(self, bridge: Bridge, stop_event: threading.Event):
        super().__init__(name="StatePusher", daemon=True)
        self.bridge = bridge
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            if self.bridge.wait_push_request(0.2):
                self.bridge.push_now()


class BarcodeConsumer(threading.Thread):
    """從實體 USB 條碼掃描器讀條碼，餵進 Bridge。"""

    def __init__(self, bridge: Bridge, scanner, stop_event: threading.Event):
        super().__init__(name="BarcodeScannerConsumer", daemon=True)
        self.bridge = bridge
        self.scanner = scanner
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                evt = self.scanner.out_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.bridge.on_hardware_barcode(evt.code)
            except Exception:  # noqa: BLE001
                logger.exception("處理條碼時發生未預期例外（條碼：%s）", evt.code)


class UartConsumer(threading.Thread):
    """讀真的 UART 封包（BNO080 + PMW3901 + HX711 三合一），把 hx711_raw 換算成
    公克餵給狀態機，同時把 yaw_deg 餵給 GateMonitor 做門口判定的航向交叉驗證。

    封包斷流超過 `stale_timeout` 就送一次 SensorDisconnected，恢復後再送
    SensorReconnected，讓畫面上的感測器燈號跟著變。
    """

    def __init__(
        self,
        bridge: Bridge,
        uart,
        gate_monitor: GateMonitor,
        offset: float,
        scale: float,
        stale_timeout: float,
        stop_event: threading.Event,
        odometry: Optional[OdometryEngine] = None,
    ):
        super().__init__(name="UartWeightConsumer", daemon=True)
        self.odometry = odometry
        self.bridge = bridge
        self.uart = uart
        self.gate_monitor = gate_monitor
        self.offset = offset
        self.scale = scale
        self.stale_timeout = stale_timeout
        self.stop_event = stop_event

    def run(self) -> None:
        last_packet_time = time.time()
        marked_disconnected = False
        while not self.stop_event.is_set():
            try:
                packet = self.uart.out_queue.get(timeout=0.2)
            except queue.Empty:
                if time.time() - last_packet_time > self.stale_timeout and not marked_disconnected:
                    marked_disconnected = True
                    self.bridge.on_sensor_connection_changed(False)
                continue
            last_packet_time = time.time()
            try:
                if marked_disconnected:
                    marked_disconnected = False
                    self.bridge.on_sensor_connection_changed(True)
                self.gate_monitor.update_heading(packet.yaw_deg)
                self.bridge.on_weight_sample(raw_to_grams(packet.hx711_raw, self.offset, self.scale))
                if self.odometry is not None:
                    est = self.odometry.process_packet(packet)
                    self.bridge.on_pose(est.x_mm, est.y_mm, est.yaw_deg, time.time())
            except Exception:  # noqa: BLE001
                logger.exception("處理 UART 封包時發生未預期例外")


class VisionConsumer(threading.Thread):
    """前方相機：每張影像找定位點標記與貨架消失點（core/store/vision.py），結果交給 Bridge。"""

    def __init__(self, bridge: Bridge, camera, detector, vcfg: dict, stop_event: threading.Event):
        super().__init__(name="VisionConsumer", daemon=True)
        self.bridge = bridge
        self.camera = camera
        self.detector = detector
        self.cfg = vcfg
        self.stop_event = stop_event

    def run(self) -> None:
        period = 1.0 / max(0.5, float(self.cfg["fps"]))
        failures = 0
        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                gray = self.camera.read_gray()
                failures = 0
                sight = self.detector.detect(gray)
                if (sight is not None and sight.ground_m <= float(self.cfg["anchor_trigger_m"])
                        and abs(sight.height_m - float(self.cfg["camera_height_m"])) <= float(self.cfg["height_tol_m"])):
                    self.bridge.on_anchor_seen(sight)
                h, w = gray.shape[:2]
                vp = store_vision.vanishing_point(store_vision.detect_lines(gray), w, h,
                                                  min_lines=int(self.cfg["vp_min_lines"]))
                if vp is not None:
                    delta = store_vision.vp_offset_deg(vp.u, vp.v, self.camera.K)
                    if abs(delta) <= float(self.cfg["vp_max_angle_deg"]):
                        self.bridge.on_vp_heading(delta)
            except Exception:  # noqa: BLE001
                failures += 1
                if failures in (1, 10, 100):
                    logger.exception("相機影像處理失敗（連續第 %d 次）", failures)
            self.stop_event.wait(max(0.0, period - (time.time() - t0)))


class PosPusher(threading.Thread):
    """每 POS_PUSH_SEC 秒把推車位置推給找貨頁地圖。"""

    def __init__(self, bridge: Bridge, stop_event: threading.Event):
        super().__init__(name="PosPusher", daemon=True)
        self.bridge = bridge
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.wait(POS_PUSH_SEC):
            self.bridge.push_pos()


class GateConsumer(threading.Thread):
    """把 BLE Beacon 的 RSSI 讀數餵進 GateMonitor，判定出穿越就通知 Bridge。"""

    def __init__(self, bridge: Bridge, scanner, gate_monitor: GateMonitor, stop_event: threading.Event):
        super().__init__(name="GateConsumer", daemon=True)
        self.bridge = bridge
        self.scanner = scanner
        self.gate_monitor = gate_monitor
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                obs = self.scanner.out_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                crossing = self.gate_monitor.process_observation(obs.beacon_id, obs.rssi, obs.timestamp)
                if crossing is not None:
                    logger.info("門口判定：%s（%s RSSI=%d）", crossing, obs.beacon_id, obs.rssi)
                    self.bridge.on_gate_crossing(crossing)
            except Exception:  # noqa: BLE001
                logger.exception("處理 BLE 觀測時發生未預期例外")


# ----------------------------------------------------------------------
