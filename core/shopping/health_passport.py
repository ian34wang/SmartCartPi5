"""
core/shopping/health_passport.py

健康護照：商品的成分標籤 × 會員的偏好（過敏原 / 葷素 / 宗教飲食）→ 這件商品有哪些衝突。

純函式，沒有 I/O。ui/bridge.py 在掃碼、畫購物清單、找貨、優惠推薦時呼叫；
tools/product_admin.py 用 TAG_OPTIONS 讓店家幫商品勾標籤。

標籤（products.tags，一個商品可以有好幾個）：
    過敏原   跟偏好精靈第一步的選項一字不差（"都沒有" 除外），直接比對
    內容物   豬肉、牛肉、其他肉類、海鮮、五辛、酒精——葷素與宗教規則用

規則（衝突 = 商品標籤 ∩ 這條規則禁止的標籤）：
    過敏原   偏好裡勾的每一項都禁止
    葷素     全素（純素）            肉、海鮮、蛋、奶、五辛
             蛋奶素                 肉、海鮮、五辛
             奶素                   肉、海鮮、蛋、五辛
             蛋素                   肉、海鮮、奶、五辛
             五辛素（植物五辛素）    肉、海鮮、蛋、奶
             鍋邊素（可接受海鮮／肉湯）肉（看不出是肉塊還是肉湯，一律提醒請自己確認）
             「肉」= 豬肉、牛肉、其他肉類；「海鮮」= 海鮮、魚類、甲殼類；
             「蛋」= 蛋；「奶」= 牛奶／羊奶
    宗教     伊斯蘭教（清真／Halal）  豬肉、酒精
             印度教、台灣民俗不食牛   牛肉

只提醒、不阻擋：購物車不會因為衝突拒絕加入（客人可能是幫別人買），畫面上標出來就好。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

ALLERGEN_TAGS = ("堅果類", "甲殼類", "芒果", "花生", "牛奶／羊奶", "蛋",
                 "芝麻", "含麩質穀物", "大豆", "魚類", "亞硫酸鹽")
CONTENT_TAGS = ("豬肉", "牛肉", "其他肉類", "海鮮", "五辛", "酒精")
TAG_OPTIONS = ALLERGEN_TAGS + CONTENT_TAGS

_MEAT = {"豬肉", "牛肉", "其他肉類"}
_SEAFOOD = {"海鮮", "魚類", "甲殼類"}
_EGG = {"蛋"}
_MILK = {"牛奶／羊奶"}
_PUNGENT = {"五辛"}

DIET_RULES: Dict[str, set] = {
    "全素（純素）": _MEAT | _SEAFOOD | _EGG | _MILK | _PUNGENT,
    "蛋奶素": _MEAT | _SEAFOOD | _PUNGENT,
    "奶素": _MEAT | _SEAFOOD | _EGG | _PUNGENT,
    "蛋素": _MEAT | _SEAFOOD | _MILK | _PUNGENT,
    "五辛素（植物五辛素）": _MEAT | _SEAFOOD | _EGG | _MILK,
    "鍋邊素（可接受海鮮／肉湯）": set(_MEAT),
}
RELIGION_RULES: Dict[str, set] = {
    "伊斯蘭教（清真／Halal）": {"豬肉", "酒精"},
    "印度教": {"牛肉"},
    "台灣民俗不食牛": {"牛肉"},
}


@dataclass(frozen=True)
class Conflict:
    tag: str        # 商品上的哪個標籤
    reason: str     # 跟哪一項偏好衝突，例如「過敏原：花生」「葷素：蛋奶素」

    def text(self) -> str:
        return f"含{self.tag}（{self.reason}）"


def conflicts(tags: Iterable[str], allergens: Sequence[str] = (), diet: Optional[str] = None,
              religion: Sequence[str] = ()) -> List[Conflict]:
    """這件商品跟偏好的全部衝突（同一個標籤可能同時違反好幾項，各列一次）。"""
    tagset = [t for t in dict.fromkeys(tags or [])]
    out: List[Conflict] = []
    for t in tagset:
        if t in (allergens or ()):
            out.append(Conflict(t, f"過敏原：{t}"))
    rule = DIET_RULES.get(diet or "")
    if rule:
        out += [Conflict(t, f"葷素：{diet}") for t in tagset if t in rule]
    for r in religion or ():
        rule = RELIGION_RULES.get(r)
        if rule:
            out += [Conflict(t, f"宗教飲食：{r}") for t in tagset if t in rule]
    return out


def summary(found: Sequence[Conflict]) -> str:
    """畫面上一行講完：含花生（過敏原：花生）、含豬肉（宗教飲食：伊斯蘭教…）"""
    return "、".join(c.text() for c in found)
