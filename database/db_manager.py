"""
database/db_manager.py

本地商品資訊庫的 SQLite 存取層。負責：
    - 建表 (products, members)
    - CRUD 操作
    - 依條碼查詢商品（core/cart_state_machine.py 掃碼比對重量時會用到）
    - 依會員代碼查詢會員（core/cart_state_machine.py 登入流程用到）

用法：
    python -m database.db_manager --init      # 建表 + 寫入測試資料（商品+會員）
    python -m database.db_manager --list      # 列出所有商品與會員

單獨執行本檔案即可初始化資料庫，不需要先啟動整個系統。

members 資料表是 Phase 4 開發購物流程狀態機時新增的（原本的交接文件只規劃了
products）。目前只存最基本的「這個會員代碼存不存在」，沒有密碼/權限這些欄
位——因為登入方式目前確定是「掃會員條碼/QR code」（不是輸入密碼），只需要
知道代碼有沒有對應到一個真實會員即可。

注意：這個 members 表是「單機本地」資料庫，如果之後系統擴充成多台購物車、
需要偵測「同一個會員是否已經在別台車登入中」，單機 SQLite 沒辦法看到其他
購物車的登入狀態，需要一個共用的中央資料庫/伺服器才能真正做到——這件事目前
還沒有對應的架構設計（開發總表沒有提到中央伺服器），所以本檔案跟
core/cart_state_machine.py 目前都只處理「這台購物車自己的登入狀態」，不處理
跨購物車重複登入偵測，先記錄在這裡，不要誤以為已經做到。
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    barcode         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    unit_price      REAL NOT NULL,
    standard_weight_g REAL NOT NULL,
    weight_tolerance_g REAL NOT NULL,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_MEMBERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    member_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# 測試會員資料，代碼格式跟 config.json 的 state_machine.login_barcode_prefix
# （預設 "MEMBER-"）對得起來，純粹是為了跟商品的 EAN-13 純數字條碼區分，
# 避免使用者不小心拿商品條碼當會員碼掃。實際會員卡/App 上的條碼格式定案後
# 要跟著改，這裡只是先讓整套流程能跑起來測試。
_SEED_MEMBERS = [
    ("MEMBER-0001", "測試會員 A"),
    ("MEMBER-0002", "測試會員 B"),
]

# 5 筆實體測試商品資料（依交接文件要求：含條碼、名稱、單價、標準重量與容差）
_SEED_PRODUCTS = [
    # barcode,          name,        unit_price, standard_weight_g, weight_tolerance_g
    ("4710018001234", "統一陽光豆漿 300ml", 25.0, 320.0, 15.0),
    ("4710428061234", "多力多滋 74g",       35.0, 74.0, 5.0),
    ("4711080012345", "泰山礦泉水 600ml",   15.0, 605.0, 10.0),
    ("4710011401234", "可口可樂 330ml 罐",  22.0, 355.0, 8.0),
    ("4710036122345", "義美小泡芙 45g",     30.0, 45.0, 4.0),
]

_DEFAULT_DB_PATH = Path(__file__).resolve().parent / "inventory.db"


@dataclass
class Product:
    barcode: str
    name: str
    unit_price: float
    standard_weight_g: float
    weight_tolerance_g: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Member:
    member_id: str
    name: str

    def to_dict(self) -> dict:
        return asdict(self)


class DBManager:
    """商品資料庫存取介面。同一個 DBManager 實例在多執行緒環境下請勿共用同一個
    sqlite3.Connection 物件；本類別每次操作都開新連線 (check_same_thread=False 亦可，
    但為求簡單明確採用 short-lived connection per call)。
    """

    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self, seed: bool = True) -> None:
        """建表；若 seed=True 且資料表目前是空的，寫入預設測試資料。"""
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.execute(_MEMBERS_SCHEMA)
            if seed:
                count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
                if count == 0:
                    conn.executemany(
                        "INSERT INTO products "
                        "(barcode, name, unit_price, standard_weight_g, weight_tolerance_g) "
                        "VALUES (?, ?, ?, ?, ?)",
                        _SEED_PRODUCTS,
                    )
                    logger.info("已寫入 %d 筆測試商品資料", len(_SEED_PRODUCTS))
                else:
                    logger.info("products 資料表已有 %d 筆資料，略過 seed", count)

                member_count = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
                if member_count == 0:
                    conn.executemany(
                        "INSERT INTO members (member_id, name) VALUES (?, ?)",
                        _SEED_MEMBERS,
                    )
                    logger.info("已寫入 %d 筆測試會員資料", len(_SEED_MEMBERS))
                else:
                    logger.info("members 資料表已有 %d 筆資料，略過 seed", member_count)

    def get_product(self, barcode: str) -> Optional[Product]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT barcode, name, unit_price, standard_weight_g, weight_tolerance_g "
                "FROM products WHERE barcode = ?",
                (barcode,),
            ).fetchone()
        return Product(**dict(row)) if row else None

    def list_products(self) -> list[Product]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT barcode, name, unit_price, standard_weight_g, weight_tolerance_g "
                "FROM products ORDER BY barcode"
            ).fetchall()
        return [Product(**dict(r)) for r in rows]

    def upsert_product(self, product: Product) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO products "
                "(barcode, name, unit_price, standard_weight_g, weight_tolerance_g) "
                "VALUES (:barcode, :name, :unit_price, :standard_weight_g, :weight_tolerance_g) "
                "ON CONFLICT(barcode) DO UPDATE SET "
                "name=excluded.name, unit_price=excluded.unit_price, "
                "standard_weight_g=excluded.standard_weight_g, "
                "weight_tolerance_g=excluded.weight_tolerance_g",
                product.to_dict(),
            )

    def delete_product(self, barcode: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM products WHERE barcode = ?", (barcode,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 會員（Phase 4 登入流程用）
    # ------------------------------------------------------------------
    def get_member(self, member_id: str) -> Optional[Member]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT member_id, name FROM members WHERE member_id = ?",
                (member_id,),
            ).fetchone()
        return Member(**dict(row)) if row else None

    def list_members(self) -> list[Member]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT member_id, name FROM members ORDER BY member_id"
            ).fetchall()
        return [Member(**dict(r)) for r in rows]

    def upsert_member(self, member: Member) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO members (member_id, name) VALUES (:member_id, :name) "
                "ON CONFLICT(member_id) DO UPDATE SET name=excluded.name",
                member.to_dict(),
            )

    def delete_member(self, member_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM members WHERE member_id = ?", (member_id,))
        return cur.rowcount > 0


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="SmartCart 商品資料庫管理工具")
    parser.add_argument("--db", default=None, help="資料庫路徑，預設 database/inventory.db")
    parser.add_argument("--init", action="store_true", help="建表並寫入測試資料（商品+會員）")
    parser.add_argument("--list", action="store_true", help="列出所有商品與會員")
    args = parser.parse_args()

    mgr = DBManager(args.db)

    if args.init:
        mgr.init_db(seed=True)

    if args.list or not args.init:
        mgr.init_db(seed=False)  # 確保表存在，不 seed
        products = mgr.list_products()
        if not products:
            print("(資料庫目前沒有商品資料，執行 --init 建立測試資料)")
        for p in products:
            print(json.dumps(p.to_dict(), ensure_ascii=False))

        members = mgr.list_members()
        if not members:
            print("(資料庫目前沒有會員資料，執行 --init 建立測試資料)")
        for m in members:
            print(json.dumps(m.to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    _main()
