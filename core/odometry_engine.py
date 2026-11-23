"""
core/odometry_engine.py

Phase 3 第一步：純 UART 版本的基礎里程計（dead-reckoning）。

依照開發總表的順序要求：「先做這個純 UART 版本並實際走一段固定距離驗證
誤差量級，再進入下一步的視覺融合」，所以這支目前只做：

    1. 從 UartReceiver 的 out_queue 拿 PMW3901 的 dx/dy（sensor 局部座標系的
       相對位移，單位是像素計數）。
    2. 用 config.json 的 odometry.optical_flow_px_to_mm 換算成公釐。
    3. 用封包裡 BNO080 已經算好的 yaw_deg（絕對航向角，度）當作目前朝向，
       把局部座標的位移向量用標準 2D 旋轉矩陣轉成全域座標系的位移。
    4. 累加進全域 (X, Y)（單位：公釐，原點是程式啟動或呼叫 reset() 那一刻）。

視覺校正（vanishing_point.py 算出的 ΔYaw_visual、依 vision_yaw_fusion_weight
加權融合）留給下一步再接，現在 yaw 就是單純採信 IMU（跟 config.json 裡
vision_yaw_fusion_weight 目前是 0 一致）。

地標校正（core/landmark_correction.py，BLE Beacon 峰值偵測/門口雙 Beacon
方向判定）是另一條獨立的修正來源，修正的是 (X, Y) 而不是 yaw，透過
apply_landmark_correction() 這個方法接進來——不是持續運作的東西，是離散地
「車子經過某個已知座標的地標時校正一次」，兩次校正之間的位置完全靠這支
檔案的 dead-reckoning 積分撐著。

squal 過濾：協定 v2 新增的 squal（PMW3901 追蹤信心值，0-255）用在這裡最
合適——信心值太低的那一筆 dx/dy 不可信，直接跳過不要累加進位置（但 yaw
還是照樣更新，因為 yaw 是 BNO080 的資料，跟 squal 無關）。

座標系與正負號（2026-11-16 起由 tools/calibrate_optical_flow.py 實測決定）：
    - 車身座標：前方 f、左方 l（右手系，逆時針為正）。
    - 全域座標：原點是開機（或 reset()）那一刻；yaw 逆時針為正。BNO080 送上來的
      yaw_deg 是相對航向（開機為 0，Game Rotation Vector），方向是不是逆時針為正
      由校正的「原地左轉」量出來：odometry.yaw_sign = +1 或 −1，用 yaw = yaw_sign × yaw_deg。
    - PMW3901 的 (dx, dy) 是感測器自己的軸，跟車身不一定對齊（裝的角度、從下往上看
      還可能左右顛倒）。換到車身座標：
          s = (dx, m·dy)                        m = −1 if odometry.flow_mirror else +1
          f =  cos φ · s_x + sin φ · s_y        φ = odometry.flow_rotation_deg
          l = −sin φ · s_x + cos φ · s_y
      φ 是「直直往前推」時感測器量到的位移方向角；m 由「往左側推」決定。
      這三個參數沒設時是 φ = 0、m = +1、yaw_sign = +1，跟舊版行為一樣。
    - 再用 yaw 把 (f, l) 轉到全域：rotate_local_to_global(f, l, yaw)。

用法（獨立測試 / 即時監看，行為風格比照其他 drivers/tools 的 CLI）：
    python3 -m core.odometry_engine --port /dev/ttyAMA0
    python3 -m core.odometry_engine --min-squal 30
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Optional

from core.position_types import (
    POSITION_SOURCE_OPTICAL_FLOW,
    YAW_SOURCE_IMU_ONLY,
    PositionEstimate,
)
from drivers.uart_receiver import UartPacket, UartReceiver

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


# ----------------------------------------------------------------------
# 純邏輯部分（可離線用假資料測試，不需要真的 UART/硬體）
# ----------------------------------------------------------------------
def sensor_to_cart(dx: float, dy: float, rotation_deg: float = 0.0, mirror: bool = False) -> tuple:
    """PMW3901 感測器軸的位移 (dx, dy) → 車身座標 (前方 f, 左方 l)。見檔案開頭的公式。"""
    sx, sy = dx, (-dy if mirror else dy)
    phi = math.radians(rotation_deg)
    c, s = math.cos(phi), math.sin(phi)
    return c * sx + s * sy, -s * sx + c * sy


def rotate_local_to_global(dx_mm: float, dy_mm: float, yaw_deg: float) -> tuple:
    """把 sensor 局部座標系的位移向量 (dx_mm, dy_mm)，依目前航向角 yaw_deg
    轉成全域座標系的位移向量 (dX_mm, dY_mm)。標準 2D 旋轉矩陣，逆時針為正：

        dX = dx*cos(yaw) - dy*sin(yaw)
        dY = dx*sin(yaw) + dy*cos(yaw)
    """
    theta = math.radians(yaw_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    dX = dx_mm * cos_t - dy_mm * sin_t
    dY = dx_mm * sin_t + dy_mm * cos_t
    return dX, dY


class OdometryEngine:
    """維護一份全域 (X, Y, yaw) 狀態，靠 process_packet() 一筆一筆餵封包更新。

    process_packet() 本身是純邏輯（沒有任何 I/O、沒有背景執行緒），方便用
    假資料單元測試；start()/stop() 是加在外面的背景執行緒包裝，從
    UartReceiver 的 out_queue 持續拉封包餵進來，供正式系統整合使用。
    """

    def __init__(
        self,
        px_to_mm: float,
        min_squal: int = 0,
        in_queue: Optional["object"] = None,
        rotation_deg: float = 0.0,
        mirror: bool = False,
        yaw_sign: int = 1,
    ):
        self.px_to_mm = px_to_mm
        self.min_squal = min_squal
        self._in_queue = in_queue
        self.rotation_deg = rotation_deg
        self.mirror = mirror
        self.yaw_sign = 1 if yaw_sign >= 0 else -1

        self._lock = threading.Lock()
        # position_source/yaw_source 目前是常數：這支引擎本身就只做純 UART
        # 光流 + 純 IMU yaw，還沒有 floor_optical_flow 備援、也還沒有視覺
        # 融合，所以永遠回報 "optical_flow" / "imu"。等之後真的接上那兩個
        # 模組，這裡才會依實際使用的資料源動態改變這兩個欄位的值——下游
        # （UI/AI/商業邏輯）現在就可以直接讀這兩個欄位，不用等那天才改。
        self._state = PositionEstimate(
            position_source=POSITION_SOURCE_OPTICAL_FLOW,
            yaw_source=YAW_SOURCE_IMU_ONLY,
        )

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @classmethod
    def from_config(cls, odometry_cfg: dict, min_squal: int = 0, in_queue=None,
                    px_to_mm: Optional[float] = None) -> "OdometryEngine":
        """用 config.json 的 odometry 區塊建立（校正工具寫回的參數都在那裡）。"""
        return cls(
            px_to_mm=odometry_cfg.get("optical_flow_px_to_mm", 1.0) if px_to_mm is None else px_to_mm,
            min_squal=min_squal, in_queue=in_queue,
            rotation_deg=float(odometry_cfg.get("flow_rotation_deg", 0.0)),
            mirror=bool(odometry_cfg.get("flow_mirror", False)),
            yaw_sign=int(odometry_cfg.get("yaw_sign", 1)),
        )

    # ------------------------------------------------------------------
    def process_packet(self, packet: UartPacket) -> PositionEstimate:
        """處理一筆封包，回傳更新後的狀態快照（thread-safe）。"""
        with self._lock:
            if packet.squal < self.min_squal:
                # 這筆光流讀數信心不足，位置不累加，但 yaw 是 BNO080 給的，
                # 跟 PMW3901 追蹤品質無關，還是照樣更新，避免朝向卡住不動。
                self._state.skipped_low_confidence_count += 1
                self._state.yaw_deg = self.yaw_sign * packet.yaw_deg
            else:
                f, l = sensor_to_cart(packet.dx, packet.dy, self.rotation_deg, self.mirror)
                dX, dY = rotate_local_to_global(f * self.px_to_mm, l * self.px_to_mm, self._state.yaw_deg)
                self._state.x_mm += dX
                self._state.y_mm += dY
                self._state.yaw_deg = self.yaw_sign * packet.yaw_deg

            self._state.sample_count += 1
            self._state.timestamp = packet.timestamp

            return self._snapshot_locked()

    def get_state(self) -> PositionEstimate:
        with self._lock:
            return self._snapshot_locked()

    def apply_landmark_correction(
        self,
        x_mm: float,
        y_mm: float,
        timestamp: float,
        landmark_id: Optional[str] = None,
        blend_weight: float = 1.0,
    ) -> PositionEstimate:
        """收到一次地標校正（core/landmark_correction.py 的 RssiPeakDetector
        偵測到峰值、或 GateCrossingDetector 確認一次門口穿越）時呼叫，把累
        積座標往這個已知地標的絕對座標 (x_mm, y_mm) 校正。

        blend_weight：1.0（預設）代表直接強制重置成地標座標——這是目前採用
        的做法，對應討論紀錄裡『強制 Reset』的建議，簡單、而且跟這個專題現
        階段要求的精度量級（數十公分）相符。小於 1.0 則是跟目前估計值做加
        權融合（簡易互補濾波，例如 0.3 代表只把 30% 的差距拉過去），這是留
        給以後如果發現地標觸發時機本身不夠穩、需要更平滑校正時的調整空間，
        目前預設值仍是直接 Reset，不需要現在就用。

        注意：這裡不會動 yaw_deg——地標校正解決的是 (X, Y) 的漂移，yaw 的
        校正是 vanishing_point.py（視覺）的責任，兩者是分開的修正來源，正
        如這支檔案開頭說明的融合策略。
        """
        if not (0.0 < blend_weight <= 1.0):
            raise ValueError("blend_weight 必須在 (0, 1] 範圍內")
        with self._lock:
            s = self._state
            s.x_mm = s.x_mm + (x_mm - s.x_mm) * blend_weight
            s.y_mm = s.y_mm + (y_mm - s.y_mm) * blend_weight
            s.last_landmark_id = landmark_id
            s.last_landmark_correction_at = timestamp
            return self._snapshot_locked()

    def reset(self) -> None:
        """歸零累積位置（yaw 保留目前值，因為那是感測器當下的實際朝向，
        不是「已走的距離」，歸零沒有意義）。校正/驗證流程（例如走一段固定
        距離量誤差）通常會在開始前呼叫這個。"""
        with self._lock:
            current_yaw = self._state.yaw_deg
            self._state = PositionEstimate(
                yaw_deg=current_yaw,
                position_source=POSITION_SOURCE_OPTICAL_FLOW,
                yaw_source=YAW_SOURCE_IMU_ONLY,
            )

    def _snapshot_locked(self) -> PositionEstimate:
        s = self._state
        return PositionEstimate(
            x_mm=s.x_mm,
            y_mm=s.y_mm,
            yaw_deg=s.yaw_deg,
            position_source=s.position_source,
            yaw_source=s.yaw_source,
            sample_count=s.sample_count,
            skipped_low_confidence_count=s.skipped_low_confidence_count,
            timestamp=s.timestamp,
            last_landmark_id=s.last_landmark_id,
            last_landmark_correction_at=s.last_landmark_correction_at,
        )

    # ------------------------------------------------------------------
    # 背景執行緒包裝（正式系統整合用；純邏輯測試不需要用到這部分）
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._in_queue is None:
            raise RuntimeError("沒有指定 in_queue，無法啟動背景執行緒（純邏輯測試請直接呼叫 process_packet）")
        if self._thread is not None:
            raise RuntimeError("OdometryEngine 已經在執行中")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="OdometryEngineThread", daemon=True)
        self._thread.start()
        logger.info("OdometryEngine 背景執行緒已啟動")

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("OdometryEngine 背景執行緒已停止")

    def _run_loop(self) -> None:
        import queue as _queue

        while not self._stop_event.is_set():
            try:
                packet = self._in_queue.get(timeout=0.1)
            except _queue.Empty:
                continue
            self.process_packet(packet)


# ----------------------------------------------------------------------
def load_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------------------
def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    default_port = "/dev/ttyAMA0"
    default_baud = 115200
    default_px_to_mm = 1.0
    try:
        cfg = load_config()
        default_port = cfg.get("serial", {}).get("port", default_port)
        default_baud = cfg.get("serial", {}).get("baudrate", default_baud)
        default_px_to_mm = cfg.get("odometry", {}).get("optical_flow_px_to_mm", default_px_to_mm)
    except (OSError, json.JSONDecodeError):
        cfg = {}

    parser = argparse.ArgumentParser(description="Phase 3 基礎里程計即時監看工具（純 UART dead-reckoning）")
    parser.add_argument("--port", default=default_port, help=f"UART 裝置節點（預設讀 config.json，目前為 {default_port}）")
    parser.add_argument("--baud", type=int, default=default_baud, help="鮑率")
    parser.add_argument("--px-to-mm", type=float, default=default_px_to_mm, help="覆蓋 config.json 的 optical_flow_px_to_mm")
    parser.add_argument("--min-squal", type=int, default=0, help="低於這個 squal 的樣本不計入位置累積，預設 0（不過濾）")
    parser.add_argument("--print-every", type=int, default=20, help="每收到幾筆封包印一次狀態，預設 20（約 1 秒一次）")
    args = parser.parse_args()

    if args.px_to_mm == 1.0:
        print(
            "[警告] optical_flow_px_to_mm 目前是 1.0（config.json 的 CALIBRATE_ME 佔位值），"
            "算出來的 (X, Y) 距離不是真實公釐數，只能看趨勢、不能當真實距離用。"
            "先跑 tools/calibrate_optical_flow.py 校正過，或用 --px-to-mm 手動覆蓋。"
        )

    receiver = UartReceiver(port=args.port, baudrate=args.baud)
    engine = OdometryEngine.from_config(cfg.get("odometry", {}), min_squal=args.min_squal,
                                        in_queue=receiver.out_queue, px_to_mm=args.px_to_mm)

    receiver.start()
    engine.start()
    print(f"監聽 {args.port} @ {args.baud}，即時積分中...（Ctrl+C 結束）")

    try:
        last_printed_count = 0
        while True:
            time.sleep(0.05)
            state = engine.get_state()
            # 用「還沒印過的樣本數是否達到門檻」判斷，而不是直接對 sample_count
            # 取餘數判斷是否為 0——如果封包暫停（例如車子沒動），count 會卡在
            # 同一個數字不動，用取餘數會導致每 50ms 就重複印一次同一筆狀態。
            if state.sample_count - last_printed_count >= args.print_every:
                last_printed_count = state.sample_count
                print(
                    f"X={state.x_mm:8.1f}mm  Y={state.y_mm:8.1f}mm  "
                    f"距原點={state.distance_from_origin_mm():7.1f}mm  "
                    f"yaw={state.yaw_deg:+6.1f}  樣本數={state.sample_count}  "
                    f"低信心跳過={state.skipped_low_confidence_count}  "
                    f"來源={state.position_source}/{state.yaw_source}"
                )
    except KeyboardInterrupt:
        print("\n結束中...")
    finally:
        engine.stop()
        receiver.stop()

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
