"""
core/weight_convert.py

HX711 原始 ADC 值 -> 公克數的換算公式，從 tools/verify_weight.py 抽出來獨立
成共用模組。原因：core/cart_state_machine.py（Phase 4，購物中即時秤重比對）
現在也需要用同一個換算公式，而 core/ 不應該去 import tools/ 底下的腳本——
tools/ 是操作員互動用的 CLI 工具（校正、驗證），core/ 是系統核心邏輯，依賴
方向應該是 tools/ 可以 import core/，不能反過來。

換算公式維持跟 tools/calibrate_weight.py 校正時用的定義一致：
    grams = (raw - offset) / scale

offset/scale 兩個常數本身怎麼校正、目前是不是還是 config.json 的 CALIBRATE_ME
佔位值，都不是這個模組要管的事——這裡只負責「給定 offset/scale，把一個原始
讀數換算成公克」這個純數學運算，呼叫方（tools/calibrate_weight.py 的校正流
程、tools/verify_weight.py 的驗證流程、core/cart_state_machine.py 的即時比
對）各自負責去哪裡取得 offset/scale（通常是 config.json 的 weight 區塊）。
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
