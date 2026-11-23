"""
core/shopping/cart.py

購物清單：有哪些商品、各幾件、多少錢。什麼時候可以加減由狀態機決定（core/shopping/state_machine.py
秤重比對成功才呼叫 add_item / remove_item），這裡只負責資料本身正確。

一個條碼一行；單價是實際收的價格（已套用折扣），list_price 是原價。一次掃碼 = 一件商品。
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
    unit_price: float          # 實際收的單價（已套用折扣）
    quantity: int = 0
    list_price: float = 0.0    # 原價（沒有折扣時跟 unit_price 一樣）

    @property
    def subtotal(self) -> float:
        return self.unit_price * self.quantity

    @property
    def list_subtotal(self) -> float:
        return (self.list_price or self.unit_price) * self.quantity


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
                unit_price=product.effective_price,
                quantity=quantity,
                list_price=product.unit_price,
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
        """應付金額（已套用折扣）。"""
        return sum(line.subtotal for line in self._items.values())

    def list_total(self) -> float:
        """原價合計；list_total − total_price 就是折扣省下的錢。"""
        return sum(line.list_subtotal for line in self._items.values())

    def list_items(self) -> List[CartLineItem]:
        return list(self._items.values())

    def clear(self) -> None:
        self._items.clear()
