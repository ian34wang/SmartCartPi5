"""
core/shopping/weight.py

HX711 原始讀數 → 公克：
    w = (r − a) / b
r 原始 ADC 讀數（count）、a = weight.hx711_offset（空秤讀數）、b = weight.hx711_scale（count/g）。
a、b 由 tools/calibrate_weight.py 多點擬合寫回 config.json。
"""

from __future__ import annotations


def raw_to_grams(raw_mean: float, offset: float, scale: float) -> float:
    """把 HX711 原始 ADC 讀數（或多筆讀數的平均值）換算成公克。

    scale 是「每公克對應多少 ADC count 差值」，等於 0 代表還沒校正過
    （config.json 的 weight.hx711_scale 預設佔位值就是 1.0，不會觸發這個錯
    誤，但如果有人手動改成 0 或校正流程算出 0，這裡要擋下來避免除以 0）。
    """
    if scale == 0:
        raise ValueError("scale 不能是 0（config.json 的 weight.hx711_scale 還沒校正過嗎？）")
    return (raw_mean - offset) / scale
