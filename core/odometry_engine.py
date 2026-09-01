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

squal 過濾：協定 v2 新增的 squal（PMW3901 追蹤信心值，0-255）用在這裡最
合適——信心值太低的那一筆 dx/dy 不可信，直接跳過不要累加進位置（但 yaw
還是照樣更新，因為 yaw 是 BNO080 的資料，跟 squal 無關）。

旋轉矩陣的座標系與正負號約定（先講清楚，避免以後接視覺校正時混淆）：
    - yaw_deg 是全域座標系下的航向角，遵循數學慣例逆時針為正（跟 BNO080
      韌體端輸出的定義要對得起來——這點要跟韌體端/實際走位測試互相印證，
      如果實測發現方向轉錯，多半是這裡的正負號跟 BNO080 的定義對不起來，
      不是積分邏輯本身有問題）。
    - dx 是 sensor 局部座標系裡「車頭正前方」的位移，dy 是「車身右側」的
      位移（PMW3901 實際的軸向對應要以貼在車上的方向為準，如果跟這裡假設
      的不一樣，把 dx/dy 對調或加負號即可，不影響其他邏輯）。

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
    ):
        self.px_to_mm = px_to_mm
        self.min_squal = min_squal
        self._in_queue = in_queue

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

    # ------------------------------------------------------------------
    def process_packet(self, packet: UartPacket) -> PositionEstimate:
        """處理一筆封包，回傳更新後的狀態快照（thread-safe）。"""
        with self._lock:
            if packet.squal < self.min_squal:
                # 這筆光流讀數信心不足，位置不累加，但 yaw 是 BNO080 給的，
                # 跟 PMW3901 追蹤品質無關，還是照樣更新，避免朝向卡住不動。
                self._state.skipped_low_confidence_count += 1
                self._state.yaw_deg = packet.yaw_deg
            else:
                dx_mm = packet.dx * self.px_to_mm
                dy_mm = packet.dy * self.px_to_mm
                dX, dY = rotate_local_to_global(dx_mm, dy_mm, self._state.yaw_deg)
                self._state.x_mm += dX
                self._state.y_mm += dY
                self._state.yaw_deg = packet.yaw_deg

            self._state.sample_count += 1
            self._state.timestamp = packet.timestamp

            return self._snapshot_locked()

    def get_state(self) -> PositionEstimate:
        with self._lock:
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
    engine = OdometryEngine(px_to_mm=args.px_to_mm, min_squal=args.min_squal, in_queue=receiver.out_queue)

    receiver.start()
    engine.start()
    print(f"監聽 {args.port} @ {args.baud}，即時積分中...（Ctrl+C 結束）")
    print("建議測試方式：現在歸零，把車沿一段量好的固定距離推過去，比對印出的距離跟實際量到的差多少。")

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
