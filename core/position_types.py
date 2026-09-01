"""
core/position_types.py

Phase 3 定位系統的「最終輸出格式」——這是刻意獨立出來的一個模組，只放資料
型別，不放任何邏輯或 I/O，目的是讓 Phase 4/5/6（state_machine.py、
app_gui.py 的 2D 散點圖、llm_agent.py 的 Context Builder）可以直接針對這個
穩定的格式開發，不用等 vanishing_point.py（視覺偏航角校正）、
floor_optical_flow.py（視覺光流備援）這些還沒寫的模組完成。

背景：目前只有 core/odometry_engine.py 這一個「來源」在產生 PositionEstimate
（純 UART dead-reckoning），但 Phase 3 後續還會有：
    - vanishing_point.py 算出的 ΔYaw_visual 融合進 yaw（不會換掉這個型別，
      只是 yaw_source 的值會從 "imu" 變成 "imu+vision"）。
    - floor_optical_flow.py 在 PMW3901 資料不可信時接手算 dx/dy（position_source
      的值會從 "optical_flow" 變成 "floor_optical_flow_fallback"）。
之後接進來時，下游（UI、AI、商業邏輯）的程式碼完全不用改，因為欄位形狀
沒變，只是這兩個來源欄位的值會變化，這正是獨立出這個型別的用意。

座標系與單位（開發時已跟你確認過）：
    - 原點：程式啟動/呼叫 reset() 那一刻，車子當下的位置與朝向。不是店面
      地圖上的固定座標——如果之後需要對齊店面地圖，需要另外做一個「初始
      定位」機制（例如開機時掃描入口的固定標記），目前系統沒有這個機制。
    - x_mm / y_mm：全域座標系下的位置，單位公釐，原點為上述定義。
    - yaw_deg：全域座標系下的航向角，單位度，遵循數學慣例逆時針為正
      （跟 BNO080 韌體端的正負號約定是否一致，需要實機驗證，見
      odometry_engine.py 的說明）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# position_source 欄位目前合法的值。之後 floor_optical_flow.py 接上時，
# 會在 PMW3901 資料不可信的期間把這個值換成 FALLBACK。
POSITION_SOURCE_OPTICAL_FLOW = "optical_flow"
POSITION_SOURCE_FLOOR_FALLBACK = "floor_optical_flow_fallback"

# yaw_source 欄位目前合法的值。之後 vanishing_point.py 的視覺校正接上時，
# （依 config.json 的 vision_yaw_fusion_weight > 0）會換成 FUSED。
YAW_SOURCE_IMU_ONLY = "imu"
YAW_SOURCE_IMU_VISION_FUSED = "imu+vision"


@dataclass
class PositionEstimate:
    """定位系統對外的最終輸出——UI、AI、商業邏輯都應該只依賴這個型別的
    欄位，不要去依賴是哪個模組（odometry_engine / floor_optical_flow /
    融合後的結果）產生的，這樣底層實作怎麼換都不會波及下游。
    """

    x_mm: float = 0.0
    y_mm: float = 0.0
    yaw_deg: float = 0.0

    # 這筆估計的可信度/來源標記，讓下游可以自己決定要不要對「非最佳來源」
    # 的估計做不同處理（例如 UI 顯示「定位精準度較低」提示、AI prompt 裡
    # 附註目前定位不完全可信）。
    position_source: str = POSITION_SOURCE_OPTICAL_FLOW
    yaw_source: str = YAW_SOURCE_IMU_ONLY

    # 累計統計，方便除錯/驗證用，不是定位結果本身的一部分。
    sample_count: int = 0
    skipped_low_confidence_count: int = 0
    timestamp: Optional[float] = None

    def distance_from_origin_mm(self) -> float:
        return math.hypot(self.x_mm, self.y_mm)

    def is_best_effort_estimate(self) -> bool:
        """True 代表這筆估計目前不是用最佳來源算出來的（例如正在用
        floor_optical_flow 備援、或 yaw 還沒有視覺校正），下游可以用這個
        快速判斷要不要提示使用者/降低對這筆數據的信任度，不用自己去比對
        position_source/yaw_source 個別是什麼字串。

        注意：現階段（vanishing_point.py 還沒做出來、yaw 永遠是純 IMU）這
        個方法會一直回傳 True，這是預期行為、不是 bug——如實反映了「目前
        系統還沒有視覺校正，所有估計都只能算 best-effort」的真實狀態，等
        視覺融合做出來、yaw_source 真的變成 imu+vision 後才會依實際情況變化。
        """
        return (
            self.position_source != POSITION_SOURCE_OPTICAL_FLOW
            or self.yaw_source != YAW_SOURCE_IMU_VISION_FUSED
        )
