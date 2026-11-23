"""
ui/bridge.py

畫面與流程之間的橋：前端（ui/templates/index.html）的操作與硬體事件都進到 Bridge，Bridge 轉成
core/shopping/state_machine.py 的事件，再組出前端要畫的完整畫面狀態（_state_payload）。

    前端操作    window.pywebview.api.xxx() → JsApi.xxx() → Bridge.xxx() → 回傳畫面狀態，前端 render
    硬體事件    ui/workers.py 的執行緒 → Bridge.on_xxx() → 有變化就 request_push()
                → StatePusher 執行緒呼叫 push_now()，用 evaluate_js 把畫面狀態推給前端

執行緒：前端呼叫、條碼、UART（20 Hz）、BLE、逾時檢查會同時進來，Bridge 的公開方法全部在同一把
RLock 裡執行（@_locked），狀態機一次只處理一個事件。evaluate_js 在鎖外呼叫。

狀態機之上多一層「精靈式引導」（歡迎 → 登入 → 讀取設定 → 過敏原 / 葷素 / 宗教 / 預算）：那是畫面
流程，不是營運狀態，用 self._ui_stage 另外記。

資料庫：
    會員偏好    精靈走完時存（訪客不存）；讀取設定選「沿用」時讀回來
    交易紀錄    付款時寫一筆，推車出場時補出場時間
    協助請求    呼叫店員、取消結帳、申請歸零／放棄都寫一筆；店員刷工作證（staff_barcode_prefix）
                就標成已處理，並執行待確認的歸零或放棄

秤重異常「重新校準歸零」與「放棄沒掃碼的重量變化」都等於接受籃子裡現在的重量，所以使用者按了
只送出申請，要店員刷工作證才生效。掃過碼、比對不符的那一筆可以直接放棄（穩定基準沒變，東西
如果還在籃子裡，下一筆讀數就會被抓成未掃碼的重量變化）。
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
import time
from typing import List, Optional

from core import config

from core.shopping.state_machine import (
    CartStateMachine,
    ForceLogoutRequested,
    GateEntryDetected,
    GateExitDetected,
    ItemScanned,
    LockForCheckoutRequested,
    LoginScanned,
    LogoutRequested,
    PaymentConfirmed,
    RetryWeightCheckRequested,
    SensorDisconnected,
    SensorReconnected,
    SEVERITY_INFO,
    STATE_AWAITING_EXIT,
    STATE_AWAITING_ITEM_SCAN,
    STATE_AWAITING_WEIGHT_DECREASE,
    STATE_AWAITING_WEIGHT_INCREASE,
    STATE_LOCKED_FOR_CHECKOUT,
    STATE_LOGGED_IN_OUTSIDE_ZONE,
    STATE_SESSION_CLOSED,
    STATE_SHOPPING,
    STATE_UNAUTHENTICATED,
    STATE_WEIGHT_MISMATCH_ERROR,
    StaffRezeroAuthorized,
    TimeoutTick,
    UnlockCheckoutRequested,
    VoidPendingItemRequested,
    WeightSampleReceived,
)
from core.gate.detector import GATE_CROSSING_ENTERING
from core.shopping import health_passport
from core.store import layout as store_layout
from core.store import vision as store_vision
from database.db_manager import DBManager, Member, MemberPreferences, Transaction, TransactionItem


logger = logging.getLogger(__name__)


# 精靈各步驟的畫面代號；wizard_goto() 只接受這些（傳錯字串會切到不存在的畫面，整頁空白）
_WIZARD_STAGES = ("login", "loading_profile", "allergens", "diet", "religion", "budget")

# 購物主畫面家族的狀態：在這些狀態下才可以從「我的」分頁重新走一次偏好精靈
_SHOPPING_FAMILY_STATES = (
    STATE_LOGGED_IN_OUTSIDE_ZONE,
    STATE_SHOPPING,
    STATE_AWAITING_WEIGHT_INCREASE,
    STATE_AWAITING_WEIGHT_DECREASE,
    STATE_AWAITING_ITEM_SCAN,
)

_ASSIST_REASONS = {
    "weight_alert": "秤重異常：呼叫店員協助",
    "exit_confirm": "付款後：取消本次結帳／需要協助",
    "general": "呼叫店員協助",
}
_REZERO_REASON = "秤重異常：申請重新校準歸零（需店員刷工作證確認）"
_VOID_UNSCANNED_REASON = "秤重異常：拿取／放入後沒掃碼，申請放棄（需店員刷工作證確認）"
_BUDGET_WARN_RATIO = 0.8
_PROMO_MAX = 6
# 推車不在管制區裡時，地圖上不顯示推車位置（對齊作廢）
_OUT_OF_ZONE_STATES = (STATE_UNAUTHENTICATED, STATE_LOGGED_IN_OUTSIDE_ZONE, STATE_SESSION_CLOSED)
_RECENT_TX_LIMIT = 5


def _fmt_time(t: Optional[float]) -> str:
    return "" if t is None else time.strftime("%m/%d %H:%M", time.localtime(t))


def summary_or_empty(found) -> str:
    return health_passport.summary(found) if found else ""


def _fmt_budget(b: Optional[float]) -> str:
    return "不限" if b is None else f"${b:.0f}"

# 偏好精靈的選項（跟設計稿的按鈕文字一字不差；健康護照用同一份標籤比對）
_ALLERGEN_OPTIONS = health_passport.ALLERGEN_TAGS + ("都沒有",)
_RELIGION_OPTIONS = ("無特別宗教飲食規範",) + tuple(health_passport.RELIGION_RULES)

# 訪客的會員代碼：前面接 login_barcode_prefix，才過得了狀態機的會員碼格式檢查
_GUEST_SUFFIX = "GUEST"


def _locked(fn):
    """在 Bridge 的鎖裡執行（多條執行緒同時呼叫時一次只跑一個）。"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class Bridge:
    """前端操作與硬體事件的入口。前端看得到的只有 JsApi 轉過來的那幾個方法。"""

    def __init__(self, db: DBManager, sm: CartStateMachine, window_holder: dict, gate_side=None,
                 store: Optional[dict] = None, vision: Optional[dict] = None):
        self.db = db
        self.sm = sm
        self._window_holder = window_holder
        # 回傳門口判定目前確認的一側："a" 門內 / "b" 門外 / None 還沒確認。登入當下推車已經在
        # 門內的話，直接算進場（見 _enter_if_inside）。測試時不給就是不知道。
        self._gate_side = gate_side
        # 店面平面圖（config.json 的 store 區塊，見 core/store/layout.py）與推車位置對齊
        self._lock = threading.RLock()
        self._push_requested = threading.Event()
        self._layout = store_layout.load(store)
        self._frame = store_layout.StoreFrame(self._layout)
        self._pose: Optional[tuple] = None      # 里程計最新一筆 (X mm, Y mm, yaw deg, t)
        self._pos_sent = None
        # 消失點修航向的互補濾波權重 w（config.json 的 vision.vp_yaw_weight）
        self._vp_weight = float((vision or {}).get("vp_yaw_weight", config.VISION_DEFAULTS["vp_yaw_weight"]))
        self._warned_shelf: set = set()   # 貨架代碼不在平面圖上的商品，各警告一次
        # 錢包管家：上一次提醒到哪一級（0 沒提醒、1 已過 80%、2 已超過預算），往上跨才提醒一次
        self._budget_level = 0
        self._last_error: Optional[str] = None

        # 精靈流程狀態（見檔案開頭說明，狀態機本身不知道這一層）
        self._ui_stage: Optional[str] = "main"
        self._wizard_allergens: List[str] = []
        self._wizard_diet: Optional[str] = None
        self._wizard_religion: List[str] = []
        self._budget: Optional[float] = None

        cfg = sm.cfg
        self._exit_timeout_sec = cfg.get("exit_timeout_after_checkout_sec", 180.0)
        self._login_prefix = cfg.get("login_barcode_prefix", "MEMBER-")
        prefix = self._login_prefix
        self._guest_member_id = f"{prefix}{_GUEST_SUFFIX}" if prefix else _GUEST_SUFFIX
        self.db.upsert_member(Member(member_id=self._guest_member_id, name="訪客"))
        self._staff_prefix = cfg.get("staff_barcode_prefix", "STAFF-")

        # 一次性的成功提示（前端用 toast 顯示），跟 _last_error 分開：成功的事
        # 不該用紅色錯誤條顯示。
        self._notice: Optional[str] = None
        # 秤重異常時使用者申請了「重新校準歸零」，等店員刷工作證
        self._staff_pending: Optional[str] = None
        # 從「我的」分頁重新走精靈時為 True；取消時還原成進來之前的設定
        self._editing_prefs = False
        self._prefs_snapshot = None
        # 這次付款寫進資料庫的交易編號，走出管制區時補出場時間
        self._open_tx_id: Optional[int] = None

        # 診斷用計數器（唯讀診斷面板顯示）
        self._last_scan: Optional[str] = None
        self._gate_events = 0

    # ------------------------------------------------------------------
    # 內部工具
    # ------------------------------------------------------------------
    def _reset_wizard_profile(self) -> None:
        self._wizard_allergens = []
        self._wizard_diet = None
        self._wizard_religion = []
        self._budget = None
        self._editing_prefs = False
        self._prefs_snapshot = None
        self._staff_pending = None
        self._budget_level = 0

    def _is_guest(self) -> bool:
        return self.sm.get_session().member_id == self._guest_member_id

    def _saved_prefs(self) -> Optional[MemberPreferences]:
        member_id = self.sm.get_session().member_id
        if not member_id or self._is_guest():
            return None
        return self.db.get_preferences(member_id)

    def _save_prefs(self) -> None:
        """把目前的精靈結果與預算存起來（訪客不存）。寫入失敗要講出來，但不擋購物。"""
        member_id = self.sm.get_session().member_id
        if not member_id or self._is_guest():
            return
        try:
            self.db.save_preferences(MemberPreferences(
                member_id=member_id, allergens=list(self._wizard_allergens), diet=self._wizard_diet,
                religion=list(self._wizard_religion), budget=self._budget, updated_at=time.time(),
            ))
        except sqlite3.Error as exc:
            logger.exception("偏好設定存檔失敗")
            self._last_error = f"偏好設定存檔失敗（這次購物照常，但下次不會記得）：{exc}"

    def _finish_closed_session(self) -> None:
        """推車出場後（SESSION_CLOSED）：補交易的出場時間、登出、回歡迎頁，讓下一位不用等。"""
        s = self.sm.get_session()
        if s.state != STATE_SESSION_CLOSED:
            return
        if self._open_tx_id is not None:
            try:
                self.db.mark_transaction_exited(self._open_tx_id, s.last_activity_at or time.time())
            except sqlite3.Error:
                logger.exception("交易 %s 的出場時間寫入失敗", self._open_tx_id)
            self._open_tx_id = None
        self.sm.process_event(LogoutRequested(timestamp=time.time()))
        self._reset_wizard_profile()
        self._ui_stage = "main"

    def _current_screen(self) -> str:
        state = self.sm.get_session().state
        # 秤重異常永遠優先：正在走偏好精靈也要先處理異常（處理完精靈從原本那一步繼續）
        if state == STATE_WEIGHT_MISMATCH_ERROR:
            return "weight_alert"
        if self._ui_stage is not None:
            return self._ui_stage
        if state in _SHOPPING_FAMILY_STATES:
            # 包含精靈走完、還沒推進管制區：停在購物主畫面，提示區顯示「等待進入管制區」
            return "shopping"
        if state == STATE_LOCKED_FOR_CHECKOUT:
            return "checkout"
        if state == STATE_AWAITING_EXIT:
            return "exit_confirm"
        return "main"

    def _member_name(self) -> Optional[str]:
        s = self.sm.get_session()
        if not s.member_id:
            return None
        member = self.db.get_member(s.member_id)
        return member.name if member else s.member_id

    def _exit_countdown(self) -> Optional[str]:
        s = self.sm.get_session()
        if s.state != STATE_AWAITING_EXIT or s.payment_confirmed_at is None:
            return None
        remaining = max(0.0, self._exit_timeout_sec - (time.time() - s.payment_confirmed_at))
        m, sec = divmod(int(remaining), 60)
        return f"{m:02d}:{sec:02d}"

    def _state_payload(self) -> dict:
        self._finish_closed_session()      # 先結束已出場的 session，再讀快照
        screen = self._current_screen()
        s = self.sm.get_session()
        items = self.sm.cart.list_items()
        if s.state != STATE_WEIGHT_MISMATCH_ERROR:
            self._staff_pending = None
        notice, self._notice = self._notice, None
        return {
            "screen": screen,
            "last_error": self._last_error,
            "member_id": s.member_id,
            "member_name": self._member_name(),
            "wizard_profile": {
                "allergens": self._wizard_allergens,
                "diet": self._wizard_diet,
                "religion": self._wizard_religion,
            },
            "budget": self._budget,
            "cart_items": [
                {"barcode": it.barcode, "name": it.name, "quantity": it.quantity, "subtotal": it.subtotal,
                 "list_subtotal": it.list_subtotal, "health": self._health_text(it.barcode)}
                for it in items
            ],
            "cart_count": self.sm.cart.item_count(),
            "cart_total": self.sm.cart.total_price(),
            "cart_list_total": self.sm.cart.list_total(),
            "budget_remaining": None if self._budget is None else self._budget - self.sm.cart.total_price(),
            "pending_item": self._pending_payload(s),
            "catalog": self._catalog_payload(),
            "store": store_layout.to_client(self._layout),
            "cart_pos": self._cart_pos_payload(),
            "promotions": self._promotions_payload(),
            "pending_item_barcode": s.pending_item_barcode,
            "pending_item_mode": s.pending_item_mode,
            "unscanned_baseline_g": s.unscanned_baseline_g,
            "current_weight_g": s.current_weight_g,
            "sensor_connected": s.sensor_connected,
            "has_weight_reading": s.current_weight_g is not None,
            "sm_state": s.state,
            "exit_countdown": self._exit_countdown(),
            # 精靈做完了、但 BLE 還沒判定推車進入管制區
            "awaiting_gate_entry": self._ui_stage is None and s.state == STATE_LOGGED_IN_OUTSIDE_ZONE,
            "notice": notice,
            "staff_pending": self._staff_pending,
            "assist_open": self._assist_open_payload(),
            "is_guest": bool(s.member_id) and self._is_guest(),
            "saved_profile": self._saved_profile_payload(),
            "recent_transactions": self._recent_transactions_payload(),
            # 唯讀診斷用
            "last_scan": self._last_scan,
            "gate_events": self._gate_events,
        }

    # ------------------------------------------------------------------
    # 健康護照、錢包管家、找貨、優惠
    # ------------------------------------------------------------------
    def _health(self, product) -> list:
        if product is None:
            return []
        return health_passport.conflicts(product.tags, self._wizard_allergens, self._wizard_diet,
                                         self._wizard_religion)

    def _health_text(self, barcode: str) -> str:
        try:
            return health_passport.summary(self._health(self.db.get_product(barcode)))
        except sqlite3.Error:
            logger.exception("讀商品資料失敗")
            return ""

    def _pending_payload(self, s) -> Optional[dict]:
        """正在比對的那一筆：名稱、價格、加入後的總額與預算、健康護照衝突。"""
        if not s.pending_item_barcode:
            return None
        try:
            p = self.db.get_product(s.pending_item_barcode)
        except sqlite3.Error:
            p = None
        if p is None:
            return {"barcode": s.pending_item_barcode, "name": s.pending_item_barcode}
        sign = 1 if s.pending_item_mode != "remove" else -1
        after = self.sm.cart.total_price() + sign * p.effective_price
        return {
            "barcode": p.barcode, "name": p.name, "price": p.effective_price, "list_price": p.unit_price,
            "total_after": after,
            "remaining_after": None if self._budget is None else self._budget - after,
            "health": summary_or_empty(self._health(p)) if sign > 0 else "",
        }

    def _catalog_payload(self) -> list:
        """找貨頁用：全部商品（小店的商品數量不多，一次給完）。"""
        try:
            products = self.db.list_products()
        except sqlite3.Error:
            logger.exception("讀商品資料失敗")
            return []
        lay = self._layout
        out = []
        for p in products:
            shelf = p.shelf if store_layout.is_valid(lay, p.shelf) else ""
            if p.shelf and not shelf and (p.barcode, p.shelf) not in self._warned_shelf:
                self._warned_shelf.add((p.barcode, p.shelf))
                logger.warning("商品 %s 的貨架位置 %r 不在平面圖上（代碼見 core/store/layout.py），找貨頁不顯示",
                               p.barcode, p.shelf)
            out.append({
                "barcode": p.barcode, "name": p.name, "price": p.effective_price, "list_price": p.unit_price,
                "discount_pct": p.discount_pct, "shelf": shelf, "in_cart": self.sm.cart.has_item(p.barcode),
                "health": summary_or_empty(self._health(p)),
                "shelf_text": store_layout.describe(lay, shelf) if shelf else "",
                "instruction": store_layout.instruction(lay, shelf) if shelf else "",
                "route": [list(pt) for pt in store_layout.route(lay, shelf)] if shelf else [],
                "spot": list(store_layout.slot_point(lay, shelf)) if shelf else None,
            })
        return out

    def _where(self, p) -> str:
        """優惠卡片上的「位置 中架・左下」；沒設或代碼不在平面圖上就空字串。"""
        return store_layout.describe(self._layout, p.shelf) if store_layout.is_valid(self._layout, p.shelf) else ""

    def _cart_pos_payload(self) -> Optional[dict]:
        pos, status = self._frame.last, self._frame.status
        if pos is None or status == store_layout.StoreFrame.NONE:
            return {"status": status}
        return {"x": round(pos[0], 2), "y": round(pos[1], 2), "status": status}

    def _promotions_payload(self) -> list:
        """優惠頁的卡片，全部從資料庫算出來：
            特價  有設 discount_pct 的商品（已在購物車的標「已套用」）
            搭配  購物車裡的商品設定的 pairs_with，還沒拿的
            推薦  跟偏好沒有衝突、而且買得起（不超過剩餘預算）的商品，還沒拿的、有貨架位置的
        健康護照衝突的商品不會出現在搭配與推薦裡。
        """
        try:
            products = {p.barcode: p for p in self.db.list_products()}
        except sqlite3.Error:
            logger.exception("讀商品資料失敗")
            return []
        cart = self.sm.cart
        remaining = None if self._budget is None else self._budget - cart.total_price()
        cards, seen = [], set()
        for p in products.values():
            if p.discount_pct > 0:
                saved = p.unit_price - p.effective_price
                applied = cart.has_item(p.barcode)
                cards.append({
                    "kind": "discount", "barcode": p.barcode,
                    "title": f"特價 {p.name}" + ("　已套用！" if applied else ""),
                    "body": f"原價 ${p.unit_price:.0f} → ${p.effective_price:.0f}（{100 - p.discount_pct:g} 折，"
                            f"每件省 ${saved:.0f}）" + (f"；位置 {self._where(p)}" if self._where(p) else ""),
                    "health": summary_or_empty(self._health(p)),
                    "can_nav": bool(self._where(p)) and not applied,
                })
                seen.add(p.barcode)
        for it in cart.list_items():
            src = products.get(it.barcode)
            for bc in (src.pairs_with if src else []):
                q = products.get(bc)
                if q is None or cart.has_item(bc) or bc in seen or self._health(q):
                    continue
                cards.append({"kind": "pair", "barcode": bc, "title": f"搭配推薦：{q.name}",
                              "body": f"購物車裡有「{it.name}」，要不要搭配「{q.name}」（${q.effective_price:.0f}）？"
                                      + (f"位置 {self._where(q)}" if self._where(q) else ""),
                              "health": "", "can_nav": bool(self._where(q))})
                seen.add(bc)
        picks = [p for p in products.values()
                 if p.barcode not in seen and not cart.has_item(p.barcode) and self._where(p) and not self._health(p)
                 and (remaining is None or p.effective_price <= remaining)]
        picks.sort(key=lambda p: (-p.discount_pct, p.effective_price))
        for p in picks[:max(0, _PROMO_MAX - len(cards))]:
            why = "符合你的健康設定" + ("、在剩餘預算內" if remaining is not None else "")
            cards.append({"kind": "pick", "barcode": p.barcode, "title": f"適合你：{p.name}",
                          "body": f"${p.effective_price:.0f}，{why}；位置 {self._where(p)}", "health": "", "can_nav": True})
        return cards[:_PROMO_MAX]

    def _check_budget(self) -> None:
        """錢包管家：總額往上跨過 80%、100% 各提醒一次（降回去再上來會再提醒）。"""
        if not self._budget:
            self._budget_level = 0
            return
        total = self.sm.cart.total_price()
        level = 2 if total > self._budget else 1 if total >= _BUDGET_WARN_RATIO * self._budget else 0
        if level > self._budget_level:
            if level == 2:
                self._notice = f"錢包管家：已超過本次預算 ${self._budget:.0f}（目前 ${total:.0f}，超出 ${total - self._budget:.0f}）"
            else:
                self._notice = f"錢包管家：已用掉預算的 {total / self._budget:.0%}（剩 ${self._budget - total:.0f}）"
        self._budget_level = level

    def _enter_if_inside(self) -> None:
        """剛登入時推車已經在門內（BLE 已確認在門內那一側）→ 直接算進場。

        不然要等「下一次穿越門口」才會進場，但人已經在店裡了，等不到——2026-11-16
        實測就卡在這裡。未登入就推進來的那一次，狀態機當時已經記過「未登入進入」的警告。
        """
        if self._gate_side is None or self.sm.get_session().state != STATE_LOGGED_IN_OUTSIDE_ZONE:
            return
        try:
            side = self._gate_side()
        except Exception:  # noqa: BLE001 — 讀不到就當不知道，照原本等穿越門口
            logger.exception("讀門口判定狀態失敗")
            return
        if side == "a":
            self.sm.process_event(GateEntryDetected(timestamp=time.time()))
            logger.info("登入時推車已經在門內，直接算進場")

    def _assist_open_payload(self) -> Optional[dict]:
        try:
            reqs = self.db.list_assistance_requests(status="open", limit=1)
        except sqlite3.Error:
            logger.exception("讀協助請求失敗")
            return None
        if not reqs:
            return None
        return {"reason": reqs[0].reason, "time": _fmt_time(reqs[0].created_at)}

    def _saved_profile_payload(self) -> dict:
        prefs = self._saved_prefs()
        if prefs is None:
            return {"exists": False}
        return {
            "exists": True,
            "allergens": prefs.allergens,
            "diet": prefs.diet,
            "religion": prefs.religion,
            "budget_text": _fmt_budget(prefs.budget),
            "updated": _fmt_time(prefs.updated_at),
        }

    def _recent_transactions_payload(self) -> list:
        member_id = self.sm.get_session().member_id
        if not member_id or self._is_guest():
            return []
        try:
            txs = self.db.list_transactions(member_id=member_id, limit=_RECENT_TX_LIMIT)
        except sqlite3.Error:
            logger.exception("讀交易紀錄失敗")
            return []
        return [{"id": t.id, "paid_at": _fmt_time(t.paid_at), "total": t.total, "item_count": t.item_count,
                 "method": t.payment_method or ""} for t in txs]

    def _alert_marker(self):
        """記住「呼叫狀態機之前，最新的一筆警告是哪一個物件」。

        為什麼不是單純記筆數：`CartStateMachine._alert()` 的歷史清單有上限
        （預設 200 筆），滿了之後每新增一筆就從頭砍掉一筆，總長度固定不變
        ——也就是說「呼叫後筆數有沒有變多」這種判斷法，在累積滿 200 筆之後
        會永遠是 False，新的錯誤就再也不會被顯示出來，畫面又會退回「操作了
        沒反應也沒錯誤」。一台整天開著的購物車跑滿 200 筆並不難。
        """
        alerts = self.sm.get_alerts()
        return alerts[-1] if alerts else None

    def _new_alert_since(self, marker):
        """回傳這次呼叫新產生的最後一筆警告；沒有新警告則回傳 None。"""
        alerts = self.sm.get_alerts()
        if not alerts:
            return None
        return alerts[-1] if alerts[-1] is not marker else None

    def _apply(self, event, surface_info: bool = False) -> dict:
        """送一個事件進狀態機，回傳最新的完整畫面狀態。

        `CartStateMachine` 設計上「被拒絕的事件」不會拋例外，只會記一筆警告然
        後直接 return——所以這裡一定要主動比對前後的警告，把新的警告當成這次
        操作的結果顯示出來，不然使用者會看到「操作了、畫面完全沒反應、也沒有
        任何錯誤訊息」。

        `surface_info`：預設只顯示 warning/critical，因為 INFO 等級大多是背景
        事件被忽略的雜訊（例如在不相干的狀態收到一筆感測器事件），跳出來只會
        干擾。但「使用者明確做了一個動作」的情況要設成 True——刷條碼就是典型：
        在還沒進管制區、或上一筆還在比對中的時候刷條碼，狀態機只會記一筆 INFO
        然後忽略，使用者眼中就是「刷了完全沒反應」，這正是要避免的情況。
        """
        self._last_error = None
        marker = self._alert_marker()
        self.sm.process_event(event)
        new_alert = self._new_alert_since(marker)
        if new_alert is not None and (surface_info or new_alert.severity != SEVERITY_INFO):
            self._last_error = new_alert.message
        return self._state_payload()

    def _fail(self, message: str) -> dict:
        self._last_error = message
        return self._state_payload()

    def _push_state(self) -> None:
        """硬體事件造成的變化：請 StatePusher 執行緒把最新畫面推給前端（不在鎖裡呼叫 evaluate_js）。"""
        self._push_requested.set()

    def wait_push_request(self, timeout: float) -> bool:
        """StatePusher 用：等到有人要求推送（或逾時）。"""
        if self._push_requested.wait(timeout):
            self._push_requested.clear()
            return True
        return False

    def push_now(self) -> None:
        """把目前畫面狀態推給前端。視窗還沒建立就跳過。"""
        window = self._window_holder.get("window")
        if window is None:
            return
        with self._lock:
            payload = self._state_payload()
        try:
            # ensure_ascii=True 是必要的：pywebview GTK 版 evaluate_js 把「字元數」當成「位元組數」傳給
            # WebKit，腳本裡有中文（UTF-8 一字 3 bytes）就會被截斷成語法錯誤，畫面停在舊狀態
            # （2026-11-16 實測：會員名稱是中文時，進場後畫面一直顯示等待進入管制區）。全部 ASCII 就沒事。
            window.evaluate_js(f"window.APP && window.APP.render({json.dumps(payload, ensure_ascii=True)})")
        except Exception:  # noqa: BLE001 — 推送失敗不該讓背景執行緒掛掉
            logger.exception("推送畫面狀態到前端失敗")

    # ------------------------------------------------------------------
    # 畫面狀態查詢
    # ------------------------------------------------------------------
    @_locked
    def get_state(self) -> dict:
        return self._state_payload()

    # ------------------------------------------------------------------
    # 歡迎頁 / 登入
    # ------------------------------------------------------------------
    @_locked
    def go_to_login(self) -> dict:
        self._last_error = None
        self._ui_stage = "login"
        return self._state_payload()

    @_locked
    def login(self, member_id: str) -> dict:
        self._last_error = None
        member_id = (member_id or "").strip()
        if not member_id:
            return self._fail("請輸入會員代碼")
        marker = self._alert_marker()
        self.sm.process_event(LoginScanned(member_id=member_id, timestamp=time.time()))
        new_alert = self._new_alert_since(marker)
        if new_alert is not None:
            # `_on_login()` 登入成功時不會產生任何警告，所以「有新警告」就等於
            # 被拒絕（條碼格式不對／查無會員／已經有人登入著）。訊息沿用狀態機
            # 寫好的原因，不要在這裡重複判斷一次規則。
            return self._fail(new_alert.message)
        self._enter_if_inside()
        self._ui_stage = "loading_profile"
        return self._state_payload()

    @_locked
    def continue_as_guest(self) -> dict:
        self._last_error = None
        self.sm.process_event(LoginScanned(member_id=self._guest_member_id, timestamp=time.time()))
        self._enter_if_inside()
        self._ui_stage = "loading_profile"
        return self._state_payload()

    @_locked
    def loading_profile_choice(self, choice: str) -> dict:
        self._reset_wizard_profile()
        self._last_error = None
        if choice == "A":
            prefs = self._saved_prefs()
            if prefs is not None:
                # 沿用已儲存的設定，只要確認本次預算（設計稿情境 A 的流程）
                self._wizard_allergens = list(prefs.allergens)
                self._wizard_diet = prefs.diet
                self._wizard_religion = list(prefs.religion)
                self._budget = prefs.budget
                self._ui_stage = "budget"
                return self._state_payload()
            if self._is_guest():
                self._last_error = "訪客沒有已儲存的設定，請照精靈設定（訪客的設定只用在這次購物，不會存起來）"
            else:
                self._last_error = "這個會員還沒有儲存過設定，先照精靈設定一次——走完會自動存起來，下次選 A 就能直接沿用"
        self._ui_stage = "allergens"
        return self._state_payload()

    # ------------------------------------------------------------------
    # 偏好精靈
    # ------------------------------------------------------------------
    @_locked
    def wizard_toggle_allergen(self, name: str) -> dict:
        self._last_error = None
        if name not in _ALLERGEN_OPTIONS:
            return self._fail(f"不認得的過敏原選項：{name}")
        if name in self._wizard_allergens:
            self._wizard_allergens.remove(name)
        else:
            self._wizard_allergens.append(name)
        return self._state_payload()

    @_locked
    def wizard_toggle_religion(self, name: str) -> dict:
        self._last_error = None
        if name not in _RELIGION_OPTIONS:
            return self._fail(f"不認得的宗教飲食選項：{name}")
        if name in self._wizard_religion:
            self._wizard_religion.remove(name)
        else:
            self._wizard_religion.append(name)
        return self._state_payload()

    @_locked
    def wizard_set_diet(self, value: str) -> dict:
        self._last_error = None
        self._wizard_diet = value
        return self._state_payload()

    @_locked
    def wizard_goto(self, stage: str) -> dict:
        if stage not in _WIZARD_STAGES:
            return self._fail(f"不認得的精靈步驟：{stage}")
        self._last_error = None
        if stage == "login" and self._editing_prefs:
            # 從「我的」進來重新設定，在第一步按「上一步」＝取消，還原原本的設定
            self._wizard_allergens, self._wizard_diet, self._wizard_religion, self._budget = self._prefs_snapshot
            self._editing_prefs = False
            self._prefs_snapshot = None
            self._ui_stage = None
            return self._state_payload()
        self._ui_stage = stage
        return self._state_payload()

    @staticmethod
    def _parse_budget(value: str):
        """回傳 (預算或 None=不限, 錯誤訊息或 None)。"""
        value = (value or "").strip()
        if value in ("unlimited", ""):
            return None, None
        try:
            budget = float(value)
        except (TypeError, ValueError):
            return None, f"預算金額看不懂：{value!r}，請輸入數字或選擇「不限」"
        if budget < 0:
            return None, "預算不能是負數"
        return budget, None

    @_locked
    def set_budget(self, value: str) -> dict:
        self._last_error = None
        budget, err = self._parse_budget(value)
        if err:
            return self._fail(err)
        self._budget = budget
        was_editing = self._editing_prefs
        self._editing_prefs = False
        self._prefs_snapshot = None
        self._save_prefs()
        if was_editing and not self._last_error:
            self._notice = "偏好設定已更新"
        # 精靈到此結束。**不會**在這裡自己觸發進場——進入管制區是由真的 BLE
        # 雙 Beacon 判定的（見 ui/workers.py 的 GateConsumer），這裡只是把畫面交給購物主畫面，
        # 狀態機仍然停在「已登入、未進管制區」，畫面會顯示等待提示。
        self._ui_stage = None
        return self._state_payload()

    # ------------------------------------------------------------------
    # 「我的」分頁
    # ------------------------------------------------------------------
    @_locked
    def set_session_budget(self, value: str) -> dict:
        """購物中直接改本次預算（會員的話也存成下次的預設）。"""
        self._last_error = None
        budget, err = self._parse_budget(value)
        if err:
            return self._fail(err)
        self._budget = budget
        self._save_prefs()
        if not self._last_error:
            self._notice = f"本次預算改為 {_fmt_budget(budget)}"
        self._budget_level = 0
        self._check_budget()
        return self._state_payload()

    @_locked
    def edit_preferences(self) -> dict:
        """從「我的」分頁重新走一次偏好精靈（過敏原 → 葷素 → 宗教 → 預算）。"""
        if self._ui_stage is not None or self.sm.get_session().state not in _SHOPPING_FAMILY_STATES:
            return self._fail("現在不能修改偏好設定（結帳、秤重異常處理中時請先完成）")
        self._last_error = None
        self._prefs_snapshot = (list(self._wizard_allergens), self._wizard_diet,
                                list(self._wizard_religion), self._budget)
        self._editing_prefs = True
        self._ui_stage = "allergens"
        return self._state_payload()

    # ------------------------------------------------------------------
    # 結帳 / 秤重異常
    # ------------------------------------------------------------------
    @_locked
    def lock_checkout(self) -> dict:
        return self._apply(LockForCheckoutRequested(timestamp=time.time()))

    @_locked
    def unlock_checkout(self) -> dict:
        return self._apply(UnlockCheckoutRequested(timestamp=time.time()))

    @_locked
    def confirm_payment(self, method: str = "") -> dict:
        before = self.sm.get_session().state
        self._apply(PaymentConfirmed(timestamp=time.time()))
        s = self.sm.get_session()
        if before == STATE_LOCKED_FOR_CHECKOUT and s.state == STATE_AWAITING_EXIT:
            self._record_transaction(method)
        return self._state_payload()

    def _record_transaction(self, method: str) -> None:
        s = self.sm.get_session()
        items = self.sm.cart.list_items()
        tx = Transaction(
            member_id=s.member_id or "", is_guest=self._is_guest(),
            session_started_at=s.session_started_at, zone_entered_at=s.zone_entered_at,
            paid_at=s.payment_confirmed_at or time.time(),
            total=self.sm.cart.total_price(), item_count=self.sm.cart.item_count(),
            payment_method=(method or "").strip() or None, budget=self._budget,
            items=[TransactionItem(barcode=it.barcode, name=it.name, unit_price=it.unit_price,
                                   quantity=it.quantity, subtotal=it.subtotal) for it in items],
        )
        try:
            self._open_tx_id = self.db.record_transaction(tx)
        except sqlite3.Error as exc:
            logger.exception("交易紀錄寫入失敗")
            self._open_tx_id = None
            self._last_error = f"付款完成，但交易紀錄寫入失敗（請通知店員記錄）：{exc}"

    # ------------------------------------------------------------------
    # 店員協助
    # ------------------------------------------------------------------
    def _create_request(self, reason: str) -> bool:
        """寫一筆協助請求。同樣原因已經有一筆待處理的就不重複寫。回傳是否成功。"""
        s = self.sm.get_session()
        try:
            if any(r.reason == reason for r in self.db.list_assistance_requests(status="open")):
                self._notice = "已經通知過店員了，請在原地稍候"
                return True
            self.db.create_assistance_request(
                created_at=time.time(), member_id=s.member_id, sm_state=s.state,
                screen=self._current_screen(), reason=reason,
                cart=[{"barcode": it.barcode, "name": it.name, "quantity": it.quantity}
                      for it in self.sm.cart.list_items()],
            )
        except sqlite3.Error as exc:
            logger.exception("協助請求寫入失敗")
            self._last_error = f"通知店員失敗（資料庫寫入錯誤），請直接找店員：{exc}"
            return False
        return True

    @_locked
    def request_assistance(self, kind: str = "general") -> dict:
        if kind not in _ASSIST_REASONS:
            return self._fail(f"不認得的協助類型：{kind}")
        self._last_error = None
        if self._create_request(_ASSIST_REASONS[kind]) and self._notice is None:
            self._notice = "已通知店員，請在原地稍候（店員到場刷工作證即完成處理）"
        return self._state_payload()

    @_locked
    def request_rezero(self) -> dict:
        if self.sm.get_session().state != STATE_WEIGHT_MISMATCH_ERROR:
            return self._fail("目前不是秤重異常，不需要重新校準歸零")
        self._last_error = None
        if self._create_request(_REZERO_REASON):
            self._staff_pending = "rezero"
            if self._notice is None:
                self._notice = "已送出歸零申請，請店員刷工作證確認"
        return self._state_payload()

    @_locked
    def on_staff_badge(self, code: str) -> None:
        """店員刷工作證：確認身分 → 執行待確認的動作（重新校準歸零）→ 這台車的協助請求標成已處理。"""
        staff = self.db.get_staff(code)
        if staff is None:
            self._last_error = (f"工作證 {code} 不在店員名單裡"
                                "（用 python3 -m tools.product_admin staff-add 建檔）")
            return
        self._last_error = None
        now = time.time()
        done = []
        if self._staff_pending in ("rezero", "void") and self.sm.get_session().state == STATE_WEIGHT_MISMATCH_ERROR:
            marker = self._alert_marker()
            if self._staff_pending == "rezero":
                self.sm.process_event(StaffRezeroAuthorized(staff_id=staff.staff_id, timestamp=now))
                ok_text, bad_text = "已重新校準歸零", "重新校準歸零失敗"
            else:
                self.sm.process_event(VoidPendingItemRequested(timestamp=now))
                logger.info("店員 %s 確認放棄未掃碼的重量變化", staff.staff_id)
                ok_text, bad_text = "已確認放棄這次動作", "放棄失敗"
            if self.sm.get_session().state == STATE_WEIGHT_MISMATCH_ERROR:
                alert = self._new_alert_since(marker)
                self._last_error = alert.message if alert else bad_text
            else:
                done.append(ok_text)
            self._staff_pending = None
        try:
            n = self.db.resolve_assistance_requests(staff.staff_id, now)
        except sqlite3.Error as exc:
            logger.exception("協助請求標記失敗")
            self._last_error = f"協助請求標記為已處理時失敗：{exc}"
            n = 0
        if n:
            done.append(f"已處理 {n} 筆協助請求")
        self._notice = f"店員 {staff.name}：" + ("、".join(done) if done else "目前沒有待處理的事項")

    @_locked
    def retry_weight(self) -> dict:
        return self._apply(RetryWeightCheckRequested(timestamp=time.time()))

    @_locked
    def void_pending(self) -> dict:
        """放棄此次動作。

        掃過碼的那一筆（比對不符）：直接放棄，不加入也不移除。這不會開漏洞——穩定基準沒變，
        東西如果還在籃子裡，下一筆讀數就會被抓成未掃碼的重量變化。
        從頭到尾沒掃碼（拿取／放入後忘記掃碼逾時）：放棄等於接受籃子裡現在的重量，跟「重新校準
        歸零」一樣，所以只送出申請，要店員刷工作證才生效（2026-11-23 改）。
        """
        s = self.sm.get_session()
        if s.state == STATE_WEIGHT_MISMATCH_ERROR and s.pending_item_barcode is None:
            self._last_error = None
            if self._create_request(_VOID_UNSCANNED_REASON):
                self._staff_pending = "void"
                if self._notice is None:
                    self._notice = "沒有掃碼的重量變化要店員確認才能放棄，已通知店員"
            return self._state_payload()
        return self._apply(VoidPendingItemRequested(timestamp=time.time()))

    # ------------------------------------------------------------------
    # 重置 / 結束程式
    # ------------------------------------------------------------------
    @_locked
    def force_logout(self, reason: str = "") -> dict:
        self._apply(ForceLogoutRequested(timestamp=time.time(), reason=reason or "工作人員手動重置"))
        self.sm.cart.clear()
        self._reset_wizard_profile()
        self._open_tx_id = None
        self._ui_stage = "main"
        with self._lock:
            self._frame.clear()
        return self._state_payload()

    def quit(self) -> None:
        window = self._window_holder.get("window")
        if window is not None:
            window.destroy()

    # ------------------------------------------------------------------
    # 硬體事件入口（都由背景執行緒呼叫，結果一律用 _push_state() 推給畫面）
    # ------------------------------------------------------------------
    @_locked
    def on_hardware_barcode(self, code: str) -> None:
        """實體 USB 條碼掃描器掃到一組條碼。

        分類規則：開頭符合 config.json 的 `state_machine.staff_barcode_prefix`
        是店員工作證；符合 `login_barcode_prefix` 是會員碼（跟
        其他當商品條碼。
        """
        code = (code or "").strip()
        if not code:
            return
        self._last_scan = code
        if self._staff_prefix and code.startswith(self._staff_prefix):
            self.on_staff_badge(code)
        elif self._login_prefix and code.startswith(self._login_prefix):
            self.login(code)
        else:
            # surface_info=True：刷條碼是使用者明確做的動作，任何被拒絕的原因
            # 都要講出來，包含狀態機只記 INFO 的那幾種（還沒進管制區、上一筆
            # 秤重比對還沒完成）。
            self._apply(ItemScanned(barcode=code, timestamp=time.time()), surface_info=True)
            s = self.sm.get_session()
            if s.pending_item_barcode == code and s.pending_item_mode == "add" and self._notice is None:
                found = self._health(self.db.get_product(code))
                if found:
                    self._notice = f"健康護照提醒：{health_passport.summary(found)}"
        self._push_state()

    def _background_event(self, event) -> bool:
        """背景來的事件（秤重讀數、逾時）。大多數什麼都不會變，有狀態變化或新警告才回傳 True。"""
        before_state = self.sm.get_session().state
        marker = self._alert_marker()
        self.sm.process_event(event)
        new_alert = self._new_alert_since(marker)
        if self.sm.get_session().state == before_state and new_alert is None:
            return False
        if new_alert is not None and new_alert.severity != SEVERITY_INFO:
            self._last_error = new_alert.message
        return True

    @_locked
    def on_weight_sample(self, grams: float) -> None:
        """UART 每收到一筆換算好的重量（20 Hz）。只有比對成功／失敗這種真的有變化時才推畫面。"""
        if self._background_event(WeightSampleReceived(grams=grams, timestamp=time.time())):
            self._check_budget()
            self._push_state()

    @_locked
    def on_tick(self) -> None:
        """約每秒一次（TimeoutTicker）：讓逾時判斷照時間觸發。"""
        if self._background_event(TimeoutTick(now=time.time())):
            self._push_state()

    @_locked
    def on_gate_crossing(self, crossing: str) -> None:
        """BLE 雙 Beacon 判定出一次門口穿越（見 `core/gate/monitor.py`）。"""
        self._gate_events += 1
        now = time.time()
        if crossing == GATE_CROSSING_ENTERING:
            self._apply(GateEntryDetected(timestamp=now))
            # 推車剛進門、在長廊裡：先暫時對齊地圖，走到定位點停住再正式對齊（core/store/layout.py）
            with self._lock:
                if self._pose is not None and self.sm.get_session().state not in _OUT_OF_ZONE_STATES:
                    self._frame.enter(*self._pose[:3])
        else:
            self._apply(GateExitDetected(timestamp=now))
            with self._lock:
                self._frame.clear()
        self._push_state()

    @_locked
    def on_pose(self, x_mm: float, y_mm: float, yaw_deg: float, t: float) -> None:
        """里程計每算出一筆位置就呼叫（UART 執行緒）。"""
        out_of_zone = self.sm.get_session().state in _OUT_OF_ZONE_STATES
        with self._lock:
            self._pose = (x_mm, y_mm, yaw_deg, t)
            if out_of_zone:
                if self._frame.aligned:
                    self._frame.clear()
                return
            self._frame.feed(x_mm, y_mm)

    def _anchor_locked(self, at=None) -> bool:
        """用最新的里程計位置對齊（呼叫端持有鎖）。回傳這次是不是從沒對齊變成對齊。"""
        was = self._frame.status
        self._frame.anchor(*self._pose[:3], at=at)
        self._frame.feed(*self._pose[:2])
        return was != store_layout.StoreFrame.ANCHORED

    @_locked
    def on_anchor_seen(self, sighting) -> None:
        """相機看到地上的定位點標記（VisionConsumer，sighting 是 core/store/vision.AnchorSighting）：
        推車位置 = 定位點 − (右方, 前方)（航向固定朝店內，右方 = +x、前方 = +y），正式對齊。"""
        if self.sm.get_session().state in _OUT_OF_ZONE_STATES:
            return
        a, u = self._layout["anchor"], store_layout.unit_m(self._layout)
        at = (float(a["x"]) - sighting.lateral_m / u, float(a["y"]) - sighting.forward_m / u)
        with self._lock:
            if self._pose is None:
                return
            first = self._anchor_locked(at)
        if first:
            self._notice = f"已在{a['name']}對齊地圖，推車位置會顯示在找貨頁"
            logger.info("看到定位點標記（前方 %.2f m、右方 %.2f m、相機高 %.2f m），地圖對齊完成",
                        sighting.forward_m, sighting.lateral_m, sighting.height_m)
            self._push_state()

    @_locked
    def on_vp_heading(self, delta_deg: float) -> None:
        """貨架消失點的方向 δ（VisionConsumer）→ 修正平面圖航向（core/store/layout.StoreFrame.correct_heading）。"""
        with self._lock:
            if self._pose is None or self._frame.status != store_layout.StoreFrame.ANCHORED:
                return
            x, y, yaw, _ = self._pose
            theta_v = store_vision.heading_from_vp(delta_deg, self._frame.heading(yaw))
            e = self._frame.correct_heading(x, y, yaw, theta_v, self._vp_weight)
        if e is not None:
            logger.debug("消失點修正航向：誤差 %.1f°", e)

    @_locked
    def anchor_here(self) -> dict:
        """找貨頁「我在定位點」：相機沒對到標記時（例如標記被擋住），客人推到定位點手動對齊。"""
        if self.sm.get_session().state in _OUT_OF_ZONE_STATES:
            return self._fail("推車還沒進入管制區，進門後再對齊")
        with self._lock:
            if self._pose is None:
                return self._fail("還沒收到里程計資料（UART），沒辦法對齊")
            self._anchor_locked()
        self._last_error = None
        self._notice = f"已在{self._layout['anchor']['name']}對齊地圖"
        return self._state_payload()

    def push_pos(self) -> None:
        """把推車位置推給找貨頁地圖（PosPusher 定時呼叫，位置有變才推）。整頁重畫太重，只移動那一點。"""
        with self._lock:
            pos = self._cart_pos_payload()
            if pos == self._pos_sent:
                return
            self._pos_sent = pos
        window = self._window_holder.get("window")
        if window is None:
            return
        try:
            window.evaluate_js(f"window.APP && window.APP.setPos && window.APP.setPos({json.dumps(pos, ensure_ascii=True)})")
        except Exception:  # noqa: BLE001
            logger.exception("推送推車位置到前端失敗")

    @_locked
    def on_sensor_connection_changed(self, connected: bool) -> None:
        """UART 封包斷流/恢復。"""
        now = time.time()
        if connected:
            self._apply(SensorReconnected(timestamp=now))
        else:
            self._apply(SensorDisconnected(timestamp=now))
        self._push_state()



class JsApi:
    """前端 window.pywebview.api 看得到的方法，只有這些（pywebview 會把 js_api 物件的公開屬性
    遞迴展開，所以不直接把 Bridge 交出去——不然 bridge.db、bridge.sm 的方法前端也呼叫得到）。
    每個方法回傳最新的畫面狀態，前端拿去 render。"""

    def __init__(self, bridge: Bridge):
        self._bridge = bridge

    def get_state(self): return self._bridge.get_state()
    def go_to_login(self): return self._bridge.go_to_login()
    def continue_as_guest(self): return self._bridge.continue_as_guest()
    def loading_profile_choice(self, choice): return self._bridge.loading_profile_choice(choice)
    def wizard_toggle_allergen(self, name): return self._bridge.wizard_toggle_allergen(name)
    def wizard_toggle_religion(self, name): return self._bridge.wizard_toggle_religion(name)
    def wizard_set_diet(self, value): return self._bridge.wizard_set_diet(value)
    def wizard_goto(self, stage): return self._bridge.wizard_goto(stage)
    def set_budget(self, value): return self._bridge.set_budget(value)
    def set_session_budget(self, value): return self._bridge.set_session_budget(value)
    def edit_preferences(self): return self._bridge.edit_preferences()
    def lock_checkout(self): return self._bridge.lock_checkout()
    def unlock_checkout(self): return self._bridge.unlock_checkout()
    def confirm_payment(self, method=""): return self._bridge.confirm_payment(method)
    def request_assistance(self, kind="general"): return self._bridge.request_assistance(kind)
    def request_rezero(self): return self._bridge.request_rezero()
    def retry_weight(self): return self._bridge.retry_weight()
    def void_pending(self): return self._bridge.void_pending()
    def anchor_here(self): return self._bridge.anchor_here()
    def quit(self): self._bridge.quit()
