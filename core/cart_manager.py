"""
core/cart_manager.py

Phase 4：購物清單的基礎資料結構與存取。

刻意只做「清單裡有什麼、數量多少、金額多少」這件事本身：
    - 不含秤重比對/流程狀態邏輯（那些在 core/cart_state_machine.py，
      CartStateMachine 驗證秤重比對成功後才會呼叫這裡的 add_item/remove_item）。
    - 不含 LLM 推薦、預算分析這類進階功能（那些排在 Phase 6，見 README
      「前身專題（大四專題.pdf）功能取捨與 Phase 7 定位」章節——Phase 4 的
      定位是「底層架構的支持」，錢包管家等進階功能會建立在這層之上，但不是
      這層本身）。

這裡不重複做防護性檢查（例如查資料庫確認條碼存在）——信任呼叫方
（CartStateMachine）已經在呼叫 add_item()/remove_item() 之前，用
database/db_manager.py 查過 Product 資料、也用 has_item() 確認過移除的商品
確實在清單裡。這樣分工是為了讓這個類別維持單純：它只管「資料結構本身要保持
正確」，不管「什麼時候可以改這個資料結構」（那是狀態機的責任）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from database.db_manager import Product


@dataclass
class CartLineItem:
    """購物清單裡的一行——一個商品條碼對應一行，quantity 是目前的數量。"""

    barcode: str
    name: str
    unit_price: float
    quantity: int = 0

    @property
    def subtotal(self) -> float:
        return self.unit_price * self.quantity


class CartManager:
    """單一購物車工作階段（session）的購物清單。CartStateMachine 每次建立新
    的 CartSession（新的一次登入~登出）時，應該對應建立一個新的 CartManager
    實例（或呼叫 clear()），避免上一位使用者的清單殘留到下一位。
    """

    def __init__(self) -> None:
        self._items: Dict[str, CartLineItem] = {}

    def add_item(self, product: Product, quantity: int = 1) -> None:
        """加入商品。如果這個條碼已經在清單裡，數量累加，不會出現同一條碼兩行。

        quantity 預設 1，因為目前的秤重比對模型是「一次掃碼 = 一次重量變化 =
        一件商品」（見 cart_state_machine.py 的說明），還沒有支援「一次掃碼、
        一次拿好幾件同款商品」這種情境。
        """
        if quantity <= 0:
            raise ValueError("quantity 必須是正整數")
        line = self._items.get(product.barcode)
        if line is None:
            self._items[product.barcode] = CartLineItem(
                barcode=product.barcode,
                name=product.name,
                unit_price=product.unit_price,
                quantity=quantity,
            )
        else:
            line.quantity += quantity

    def remove_item(self, barcode: str, quantity: int = 1) -> None:
        """移除商品。數量歸零時整行從清單刪除（而不是留一行 quantity=0）。

        呼叫前應該先用 has_item() 確認條碼存在，這裡查不到會直接丟
        KeyError——因為「移除一個清單裡沒有的商品」代表呼叫方（狀態機）的
        防護邏輯出了漏洞，不應該悄悄吞掉，讓問題盡早被抓到。
        """
        line = self._items.get(barcode)
        if line is None:
            raise KeyError(f"購物清單裡沒有條碼 {barcode}（呼叫方應該先用 has_item() 檢查）")
        if quantity <= 0:
            raise ValueError("quantity 必須是正整數")
        line.quantity -= quantity
        if line.quantity <= 0:
            del self._items[barcode]

    def has_item(self, barcode: str) -> bool:
        return barcode in self._items

    def get_item(self, barcode: str) -> CartLineItem | None:
        return self._items.get(barcode)

    def item_count(self) -> int:
        """清單裡所有商品的總件數（不是行數，是數量加總）。"""
        return sum(line.quantity for line in self._items.values())

    def total_price(self) -> float:
        return sum(line.subtotal for line in self._items.values())

    def list_items(self) -> List[CartLineItem]:
        return list(self._items.values())

    def clear(self) -> None:
        self._items.clear()
