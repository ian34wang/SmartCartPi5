"""
core/positioning/odometry.py

里程計（dead-reckoning）：PMW3901 光流的位移 + BNO080 的航向，一筆一筆累加成推車位置。
純邏輯；app 在 UART 執行緒裡每收到一筆封包呼叫一次 process_packet()。

座標系：
    車身座標  前方 f、左方 l
    全域座標  (X, Y) 公釐，原點是程式啟動（或 reset()）那一刻；航向 ψ 逆時針為正（左轉為正）
    BNO080 的 yaw_deg 是相對航向（開機為 0，Game Rotation Vector）

一筆封包 (dx, dy, squal, yaw_deg)：
    1. 感測器軸 → 車身（PMW3901 不一定跟車身對齊，從下往上看還可能左右顛倒）：
           s = (dx, m·dy)                      m = −1 若 odometry.flow_mirror，否則 +1
           f =  cos φ · s_x + sin φ · s_y       φ = odometry.flow_rotation_deg（直推時感測器量到的方向角）
           l = −sin φ · s_x + cos φ · s_y
    2. 換成公釐：乘上 k = odometry.optical_flow_px_to_mm（mm/count）
    3. 車身 → 全域，用「上一筆」的航向 ψ（區間起點的航向，標準的積分近似）：
           ΔX = k·(f cos ψ − l sin ψ),   ΔY = k·(f sin ψ + l cos ψ)
    4. 更新航向 ψ = yaw_sign · yaw_deg（odometry.yaw_sign：原地左轉時 yaw 增加為 +1）
    squal < min_squal 的那一筆光流不可信，位置不累加（航向照樣更新，它是 BNO080 給的）。

k、φ、m、yaw_sign 由 tools/calibrate_optical_flow.py 實測寫回 config.json；沒設時 φ = 0、m = +1、
yaw_sign = +1、k = 1（佔位值，距離不是公釐）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Optional, Tuple


@dataclass
class PositionEstimate:
    x_mm: float = 0.0
    y_mm: float = 0.0
    yaw_deg: float = 0.0
    sample_count: int = 0
    skipped_low_confidence_count: int = 0      # squal < min_squal、沒累加位置的筆數
    timestamp: Optional[float] = None


def sensor_to_cart(dx: float, dy: float, rotation_deg: float = 0.0, mirror: bool = False) -> Tuple[float, float]:
    """PMW3901 感測器軸的位移 (dx, dy) → 車身座標 (f, l)。"""
    sx, sy = dx, (-dy if mirror else dy)
    phi = math.radians(rotation_deg)
    c, s = math.cos(phi), math.sin(phi)
    return c * sx + s * sy, -s * sx + c * sy


def rotate_local_to_global(f: float, l: float, yaw_deg: float) -> Tuple[float, float]:
    """車身座標的位移 (f, l) → 全域 (ΔX, ΔY)，航向 yaw_deg（逆時針為正）。"""
    t = math.radians(yaw_deg)
    return f * math.cos(t) - l * math.sin(t), f * math.sin(t) + l * math.cos(t)


class OdometryEngine:
    def __init__(self, px_to_mm: float, min_squal: int = 0, rotation_deg: float = 0.0,
                 mirror: bool = False, yaw_sign: int = 1):
        self.px_to_mm = px_to_mm
        self.min_squal = min_squal
        self.rotation_deg = rotation_deg
        self.mirror = mirror
        self.yaw_sign = 1 if yaw_sign >= 0 else -1
        self._state = PositionEstimate()

    @classmethod
    def from_config(cls, odometry_cfg: dict, min_squal: int = 0) -> "OdometryEngine":
        """用 config.json 的 odometry 區塊建立。"""
        return cls(
            px_to_mm=float(odometry_cfg.get("optical_flow_px_to_mm", 1.0)),
            min_squal=min_squal,
            rotation_deg=float(odometry_cfg.get("flow_rotation_deg", 0.0)),
            mirror=bool(odometry_cfg.get("flow_mirror", False)),
            yaw_sign=int(odometry_cfg.get("yaw_sign", 1)),
        )

    def process_packet(self, packet) -> PositionEstimate:
        """一筆封包（有 dx, dy, squal, yaw_deg, timestamp），回傳更新後的狀態（複本）。"""
        s = self._state
        if packet.squal < self.min_squal:
            s.skipped_low_confidence_count += 1
        else:
            f, l = sensor_to_cart(packet.dx, packet.dy, self.rotation_deg, self.mirror)
            dX, dY = rotate_local_to_global(f * self.px_to_mm, l * self.px_to_mm, s.yaw_deg)
            s.x_mm += dX
            s.y_mm += dY
        s.yaw_deg = self.yaw_sign * packet.yaw_deg
        s.sample_count += 1
        s.timestamp = packet.timestamp
        return replace(s)

    def get_state(self) -> PositionEstimate:
        return replace(self._state)

    def reset(self) -> None:
        """位置歸零（航向保留：那是感測器當下的實際朝向）。"""
        self._state = PositionEstimate(yaw_deg=self._state.yaw_deg)
