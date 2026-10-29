"""
database/db_manager.py

本地商品資訊庫的 SQLite 存取層。負責：
    - 建表 (products, members, member_preferences, transactions,
      transaction_items, staff, assistance_requests)
    - CRUD 操作
    - 依條碼查詢商品（core/cart_state_machine.py 掃碼比對重量時會用到）
    - 依會員代碼查詢會員（core/cart_state_machine.py 登入流程用到）
    - 會員偏好（過敏原／葷素／宗教飲食／上次預算）：精靈走完時存、登入選
      「情境 A」時讀（ui/app_gui.py）
    - 交易紀錄：付款完成時寫一筆，推車走出管制區時補上出場時間
    - 店員工作證與協助請求：「呼叫店員協助」寫一筆待處理紀錄，店員刷工作證
      時標記為已處理（沒有另外的店員通知系統，後台用 tools/product_admin.py 看）

所有表都是 CREATE TABLE IF NOT EXISTS，已經存在的舊資料庫在下次 init_db()
（app_gui、product_admin 啟動時都會呼叫）時自動補上新表，不用重建。
時間欄位一律存 epoch 秒（REAL），跟狀態機的 timestamp 同一種單位。

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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

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

# 另開一張表而不是在 members 上加欄位：已經存在的資料庫不用 ALTER TABLE，
# 沒有設定過的會員就是查不到這一列（＝沒有已儲存的設定），語意很清楚。
# allergens / religion 是 JSON 字串陣列，選項文字跟精靈畫面上的一模一樣。
_EXTRA_SCHEMAS = [
    """
CREATE TABLE IF NOT EXISTS member_preferences (
    member_id   TEXT PRIMARY KEY REFERENCES members(member_id) ON DELETE CASCADE,
    allergens   TEXT NOT NULL DEFAULT '[]',
    diet        TEXT,
    religion    TEXT NOT NULL DEFAULT '[]',
    budget      REAL,
    updated_at  REAL NOT NULL
);
""",
    """
CREATE TABLE IF NOT EXISTS transactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id           TEXT NOT NULL,
    is_guest            INTEGER NOT NULL DEFAULT 0,
    session_started_at  REAL,
    zone_entered_at     REAL,
    paid_at             REAL NOT NULL,
    exited_at           REAL,
    total               REAL NOT NULL,
    item_count          INTEGER NOT NULL,
    payment_method      TEXT,
    budget              REAL
);
""",
    """
CREATE TABLE IF NOT EXISTS transaction_items (
    transaction_id  INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    barcode         TEXT NOT NULL,
    name            TEXT NOT NULL,
    unit_price      REAL NOT NULL,
    quantity        INTEGER NOT NULL,
    subtotal        REAL NOT NULL
);
""",
    """
CREATE TABLE IF NOT EXISTS staff (
    staff_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
""",
    """
CREATE TABLE IF NOT EXISTS assistance_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   REAL NOT NULL,
    member_id    TEXT,
    sm_state     TEXT NOT NULL,
    screen       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    cart_json    TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'open',
    resolved_at  REAL,
    resolved_by  TEXT
);
""",
]

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

# 測試店員工作證。前綴對應 config.json 的 state_machine.staff_barcode_prefix。
_SEED_STAFF = [
    ("STAFF-0001", "測試店員"),
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


@dataclass
class MemberPreferences:
    member_id: str
    allergens: List[str] = field(default_factory=list)
    diet: Optional[str] = None
    religion: List[str] = field(default_factory=list)
    budget: Optional[float] = None      # None = 不限
    updated_at: float = 0.0


@dataclass
class TransactionItem:
    barcode: str
    name: str
    unit_price: float
    quantity: int
    subtotal: float


@dataclass
class Transaction:
    member_id: str
    paid_at: float
    total: float
    item_count: int
    items: List[TransactionItem] = field(default_factory=list)
    is_guest: bool = False
    session_started_at: Optional[float] = None
    zone_entered_at: Optional[float] = None
    exited_at: Optional[float] = None
    payment_method: Optional[str] = None
    budget: Optional[float] = None
    id: Optional[int] = None


@dataclass
class Staff:
    staff_id: str
    name: str


@dataclass
class AssistanceRequest:
    id: int
    created_at: float
    member_id: Optional[str]
    sm_state: str
    screen: str
    reason: str
    cart: list
    status: str
    resolved_at: Optional[float]
    resolved_by: Optional[str]


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
        conn.execute("PRAGMA foreign_keys = ON")
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
            for schema in _EXTRA_SCHEMAS:
                conn.execute(schema)
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

                if conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 0:
                    conn.executemany("INSERT INTO staff (staff_id, name) VALUES (?, ?)", _SEED_STAFF)
                    logger.info("已寫入 %d 筆測試店員資料", len(_SEED_STAFF))

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


    # ------------------------------------------------------------------
    # 會員偏好
    # ------------------------------------------------------------------
    def get_preferences(self, member_id: str) -> Optional[MemberPreferences]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT member_id, allergens, diet, religion, budget, updated_at "
                "FROM member_preferences WHERE member_id = ?",
                (member_id,),
            ).fetchone()
        if row is None:
            return None
        return MemberPreferences(
            member_id=row["member_id"], allergens=json.loads(row["allergens"]), diet=row["diet"],
            religion=json.loads(row["religion"]), budget=row["budget"], updated_at=row["updated_at"],
        )

    def save_preferences(self, prefs: MemberPreferences) -> None:
        """會員不存在時 SQLite 的外鍵會擋下來（sqlite3.IntegrityError），不會存一筆孤兒資料。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO member_preferences (member_id, allergens, diet, religion, budget, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(member_id) DO UPDATE SET allergens=excluded.allergens, diet=excluded.diet, "
                "religion=excluded.religion, budget=excluded.budget, updated_at=excluded.updated_at",
                (prefs.member_id, json.dumps(prefs.allergens, ensure_ascii=False), prefs.diet,
                 json.dumps(prefs.religion, ensure_ascii=False), prefs.budget, prefs.updated_at),
            )

    # ------------------------------------------------------------------
    # 交易紀錄
    # ------------------------------------------------------------------
    def record_transaction(self, tx: Transaction) -> int:
        """寫一筆交易與它的明細（同一個 transaction，要嘛全寫進去、要嘛全不寫）。回傳交易編號。"""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO transactions (member_id, is_guest, session_started_at, zone_entered_at, paid_at, "
                "exited_at, total, item_count, payment_method, budget) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tx.member_id, int(tx.is_guest), tx.session_started_at, tx.zone_entered_at, tx.paid_at,
                 tx.exited_at, tx.total, tx.item_count, tx.payment_method, tx.budget),
            )
            tx_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO transaction_items (transaction_id, barcode, name, unit_price, quantity, subtotal) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(tx_id, it.barcode, it.name, it.unit_price, it.quantity, it.subtotal) for it in tx.items],
            )
        return tx_id

    def mark_transaction_exited(self, tx_id: int, exited_at: float) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE transactions SET exited_at = ? WHERE id = ?", (exited_at, tx_id))

    def list_transactions(self, member_id: Optional[str] = None, limit: int = 20) -> List[Transaction]:
        """新的在前。member_id=None 列出全部。"""
        with self._connect() as conn:
            sql = "SELECT * FROM transactions"
            params: tuple = ()
            if member_id is not None:
                sql += " WHERE member_id = ?"
                params = (member_id,)
            rows = conn.execute(sql + " ORDER BY paid_at DESC, id DESC LIMIT ?", params + (limit,)).fetchall()
            out = []
            for r in rows:
                items = conn.execute(
                    "SELECT barcode, name, unit_price, quantity, subtotal FROM transaction_items "
                    "WHERE transaction_id = ? ORDER BY rowid", (r["id"],),
                ).fetchall()
                out.append(Transaction(
                    id=r["id"], member_id=r["member_id"], is_guest=bool(r["is_guest"]),
                    session_started_at=r["session_started_at"], zone_entered_at=r["zone_entered_at"],
                    paid_at=r["paid_at"], exited_at=r["exited_at"], total=r["total"], item_count=r["item_count"],
                    payment_method=r["payment_method"], budget=r["budget"],
                    items=[TransactionItem(**dict(i)) for i in items],
                ))
        return out

    # ------------------------------------------------------------------
    # 店員工作證
    # ------------------------------------------------------------------
    def get_staff(self, staff_id: str) -> Optional[Staff]:
        with self._connect() as conn:
            row = conn.execute("SELECT staff_id, name FROM staff WHERE staff_id = ?", (staff_id,)).fetchone()
        return Staff(**dict(row)) if row else None

    def list_staff(self) -> List[Staff]:
        with self._connect() as conn:
            rows = conn.execute("SELECT staff_id, name FROM staff ORDER BY staff_id").fetchall()
        return [Staff(**dict(r)) for r in rows]

    def upsert_staff(self, staff: Staff) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO staff (staff_id, name) VALUES (?, ?) "
                "ON CONFLICT(staff_id) DO UPDATE SET name=excluded.name",
                (staff.staff_id, staff.name),
            )

    def delete_staff(self, staff_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM staff WHERE staff_id = ?", (staff_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 協助請求
    # ------------------------------------------------------------------
    def create_assistance_request(self, created_at: float, member_id: Optional[str], sm_state: str,
                                  screen: str, reason: str, cart: list) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO assistance_requests (created_at, member_id, sm_state, screen, reason, cart_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (created_at, member_id, sm_state, screen, reason, json.dumps(cart, ensure_ascii=False)),
            )
        return cur.lastrowid

    def list_assistance_requests(self, status: Optional[str] = None, limit: int = 50) -> List[AssistanceRequest]:
        """新的在前。status = 'open' / 'resolved' / None（全部）。"""
        with self._connect() as conn:
            sql = "SELECT * FROM assistance_requests"
            params: tuple = ()
            if status is not None:
                sql += " WHERE status = ?"
                params = (status,)
            rows = conn.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", params + (limit,)).fetchall()
        return [AssistanceRequest(
            id=r["id"], created_at=r["created_at"], member_id=r["member_id"], sm_state=r["sm_state"],
            screen=r["screen"], reason=r["reason"], cart=json.loads(r["cart_json"]), status=r["status"],
            resolved_at=r["resolved_at"], resolved_by=r["resolved_by"],
        ) for r in rows]

    def resolve_assistance_requests(self, resolved_by: str, resolved_at: float,
                                    ids: Optional[List[int]] = None) -> int:
        """把待處理的請求標成已處理。ids=None 表示這台購物車所有待處理的請求
        （資料庫是單機的，裡面只有這台車的請求）。回傳處理了幾筆。"""
        with self._connect() as conn:
            if ids is None:
                cur = conn.execute(
                    "UPDATE assistance_requests SET status='resolved', resolved_at=?, resolved_by=? "
                    "WHERE status='open'", (resolved_at, resolved_by))
            else:
                marks = ",".join("?" * len(ids))
                cur = conn.execute(
                    f"UPDATE assistance_requests SET status='resolved', resolved_at=?, resolved_by=? "
                    f"WHERE status='open' AND id IN ({marks})", (resolved_at, resolved_by, *ids))
        return cur.rowcount


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
