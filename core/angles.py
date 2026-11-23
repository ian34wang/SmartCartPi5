"""
core/angles.py

角度工具（度）。
"""

from __future__ import annotations

from typing import Iterable, List


def wrap_deg(a: float) -> float:
    """包到 (−180, 180]。"""
    a = (a + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


def unwrap_deg(values: Iterable[float]) -> List[float]:
    """把跨過 ±180° 的航向接成連續的（算總共轉了幾度用）。"""
    out: List[float] = []
    for v in values:
        out.append(v if not out else out[-1] + wrap_deg(v - out[-1]))
    return out
