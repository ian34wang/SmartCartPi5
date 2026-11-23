"""
core/shopping/state_machine.py

購物流程狀態機：一台推車從登入到登出的整趟流程（掃碼、秤重比對、防損、進出管制區）。
純邏輯，除了查資料庫沒有 I/O；硬體事件由 ui/workers.py 餵進來，畫面由 ui/bridge.py 讀。

流程與狀態：
    UNAUTHENTICATED ─登入─▶ LOGGED_IN_OUTSIDE_ZONE ─BLE 判定進場─▶ SHOPPING
    SHOPPING ⇄ AWAITING_ITEM_SCAN（重量先變、等掃碼）
    SHOPPING ⇄ AWAITING_WEIGHT_INCREASE / _DECREASE（掃碼後等重量）
    以上任一步出錯 ─▶ WEIGHT_MISMATCH_ERROR（重試／放棄／店員歸零）
    SHOPPING ─鎖定─▶ LOCKED_FOR_CHECKOUT ─付款─▶ AWAITING_EXIT ─BLE 判定出場─▶ SESSION_CLOSED ─登出─▶ UNAUTHENTICATED

錯誤分兩種：
    (a) 軟體擋得下來的（條碼查無資料、秤重對不上）：擋下動作、發 CartAlert、提供重試／放棄。
    (b) 可能是防損事件的（沒結帳就出場、未登入就進場）：發 CRITICAL 警告，狀態不自動改，交給
        上層（畫面、店員）處理。狀態機攔不住人，這是軟體的邊界。
被拒絕的事件不丟例外，只記一筆 CartAlert（呼叫端比對前後的警告把結果顯示出來）。

加入／移除由「掃碼與重量變化誰先發生」自動判斷，ItemScanned 不帶模式：
    加入  先掃碼、重量還沒變 → AWAITING_WEIGHT_INCREASE，基準 = 掃碼當下的穩定重量。
    移除  先拿出來、重量先變 → AWAITING_ITEM_SCAN（記下變化前的基準 b）→ 掃碼時依目前重量比 b
          輕或重決定移除或加入，基準用 b。
    AWAITING_ITEM_SCAN 期間重量自己回到 b 附近（手滑碰到籃子）→ 自動解除；
    超過 unscanned_change_timeout_sec 沒掃碼 → WEIGHT_MISMATCH_ERROR（「只做一半」）。

秤重比對：基準 b、穩定讀數 w̃、商品標準重量 m、容差 τ，預期變化 e = +m（加入）或 −m（移除），
實測變化 δ = w̃ − b：
    |δ − e| ≤ τ                       比對成功，寫入購物清單
    sign(δ) ≠ sign(e) 且 |δ| > τ       方向相反（可能同時動了別的東西）→ 異常
    sign(δ) = sign(e) 且 |δ| > |e| + τ  超出上限（拿了不只一件或拿錯）→ 異常
    其他                              還在放／拿的過程中，繼續等；超過 weight_match_timeout_sec → 異常
購物中變化量 |w̃ − 穩定重量| < unscanned_change_threshold_g 視為雜訊，並讓穩定重量跟上小幅漂移。

穩定讀數 w̃（config.json 的 weight.settle_window_sec = W_s、settle_band_g = B_s）：
    最近 W_s 秒的讀數 w₁…wₙ，若 n ≥ 3、涵蓋至少 0.8·W_s 秒、且 max(w) − min(w) ≤ B_s，
    則 w̃ = median(w)；否則「還沒穩」，只更新目前重量、不做判斷。W_s = 0 表示每筆都算穩定。
    放入商品時讀數會先衝過頭再停（實測衝擊最多 69 g），用穩定值才不會把晃動的那一筆當成結果。

逾時用 TimeoutTick（呼叫端約每秒送一次）驅動，所以逾時邏輯也能用指定時間的事件測試。
"""

from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Union

from core import config
from core.shopping.cart import CartManager
from database.db_manager import DBManager

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 狀態（字串常數：直接放進畫面狀態 JSON、記錄檔）
# ----------------------------------------------------------------------
STATE_UNAUTHENTICATED = "unauthenticated"
STATE_LOGGED_IN_OUTSIDE_ZONE = "logged_in_outside_zone"
STATE_SHOPPING = "shopping"
STATE_AWAITING_ITEM_SCAN = "awaiting_item_scan"  # 重量已變化，等使用者掃碼指認是哪個商品
STATE_AWAITING_WEIGHT_INCREASE = "awaiting_weight_increase"
STATE_AWAITING_WEIGHT_DECREASE = "awaiting_weight_decrease"
STATE_WEIGHT_MISMATCH_ERROR = "weight_mismatch_error"
STATE_LOCKED_FOR_CHECKOUT = "locked_for_checkout"
STATE_AWAITING_EXIT = "awaiting_exit"
STATE_SESSION_CLOSED = "session_closed"

# 已經掃碼、等重量比對
_AWAITING_WEIGHT_STATES = (STATE_AWAITING_WEIGHT_INCREASE, STATE_AWAITING_WEIGHT_DECREASE)
# 秤重流程進行中（多了「重量已變、等掃碼」）：鎖定結帳、感測器斷線要涵蓋這組；能不能掃碼不能
_WEIGHT_IN_PROGRESS_STATES = _AWAITING_WEIGHT_STATES + (STATE_AWAITING_ITEM_SCAN,)
# 已進管制區、還沒付款：這時走出門口就是沒結帳出場
_IN_ZONE_UNPAID_STATES = (STATE_SHOPPING, STATE_AWAITING_ITEM_SCAN, STATE_AWAITING_WEIGHT_INCREASE,
                          STATE_AWAITING_WEIGHT_DECREASE, STATE_WEIGHT_MISMATCH_ERROR, STATE_LOCKED_FOR_CHECKOUT)

ITEM_SCAN_MODE_ADD = "add"
ITEM_SCAN_MODE_REMOVE = "remove"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

_SEVERITY_LOG_LEVEL = {
    SEVERITY_INFO: logging.INFO,
    SEVERITY_WARNING: logging.WARNING,
    SEVERITY_CRITICAL: logging.ERROR,
}

# ----------------------------------------------------------------------
# 警告代碼：每個可能出問題的地方一個代碼，畫面、記錄、統計依代碼分類，不解析訊息文字
# ----------------------------------------------------------------------
ALERT_LOGIN_FORMAT_INVALID = "login_format_invalid"
ALERT_LOGIN_MEMBER_NOT_FOUND = "login_member_not_found"
ALERT_ZONE_ENTRY_WITHOUT_LOGIN = "zone_entry_without_login"
ALERT_ZONE_ENTRY_TIMEOUT = "zone_entry_timeout"
ALERT_UNKNOWN_BARCODE = "unknown_barcode_scanned"
ALERT_ITEM_NOT_IN_CART = "item_not_in_cart"
ALERT_WEIGHT_MATCH_TIMEOUT = "weight_match_timeout"
ALERT_WEIGHT_MISMATCH = "weight_mismatch"
ALERT_WEIGHT_DIRECTION_MISMATCH = "weight_direction_mismatch"
ALERT_UNSCANNED_WEIGHT_CHANGE = "unscanned_weight_change"
ALERT_UNSCANNED_WEIGHT_TIMEOUT = "unscanned_weight_timeout"
ALERT_UNSCANNED_WEIGHT_AUTO_RESOLVED = "unscanned_weight_auto_resolved"
ALERT_SENSOR_DISCONNECTED = "sensor_disconnected"
ALERT_SENSOR_RECONNECTED = "sensor_reconnected"
ALERT_SCAN_WHILE_LOCKED = "scan_while_locked"
ALERT_LOCK_REJECTED_PENDING_WEIGHT = "lock_rejected_pending_weight_check"
ALERT_LOCK_REJECTED_UNRESOLVED_ERROR = "lock_rejected_unresolved_error"
ALERT_EXIT_WITHOUT_CHECKOUT = "exit_without_checkout"
ALERT_EXIT_TIMEOUT_AFTER_CHECKOUT = "exit_timeout_after_checkout"
ALERT_SHOPPING_IDLE = "shopping_idle_timeout"
ALERT_LOGOUT_REJECTED_SESSION_NOT_CLOSED = "logout_rejected_session_not_closed"
ALERT_FORCE_LOGOUT_WITH_ITEMS = "force_logout_with_items_abandoned"
ALERT_UNEXPECTED_EVENT = "unexpected_event_for_state"
ALERT_CHECKOUT_UNLOCKED = "checkout_unlocked"
ALERT_STAFF_REZERO = "staff_rezero_authorized"

# ----------------------------------------------------------------------
# 事件：每種外部輸入一個 dataclass。timestamp 是事件發生的時間（不是處理的時間）
# ----------------------------------------------------------------------
@dataclass
class LoginScanned:
    member_id: str
    timestamp: float

@dataclass
class GateEntryDetected:
    timestamp: float

@dataclass
class GateExitDetected:
    timestamp: float

@dataclass
class ItemScanned:
    """掃到一個商品條碼。不帶加入／移除，由狀態機依掃碼與重量變化的先後判斷。"""

    barcode: str
    timestamp: float

@dataclass
class WeightSampleReceived:
    grams: float
    timestamp: float

@dataclass
class SensorDisconnected:
    timestamp: float

@dataclass
class SensorReconnected:
    timestamp: float

@dataclass
class LockForCheckoutRequested:
    timestamp: float

@dataclass
class PaymentConfirmed:
    """付款已完成（沒有金流，結帳畫面確認後送出）。"""

    timestamp: float

@dataclass
class RetryWeightCheckRequested:
    """秤重異常時重新比對：用目前重量當新基準再等一次。"""

    timestamp: float

@dataclass
class VoidPendingItemRequested:
    """秤重異常時放棄這一筆（不加入也不移除），回到 SHOPPING。

    沒掃過碼的異常（只做一半逾時）沒有待比對的商品，放棄等於把目前重量當新基準，所以畫面上
    這種情況要店員確認才會送出（ui/bridge.py）。
    """

    timestamp: float

@dataclass
class UnlockCheckoutRequested:
    """結帳畫面「返回繼續選購」：解除鎖定回到 SHOPPING。

    鎖定期間只更新 current_weight_g、不動 stable_weight_g，解鎖後第一筆穩定讀數就跟鎖定前的
    穩定重量比，鎖定期間放進或拿出的東西一樣會被抓成未掃碼的重量變化。
    """

    timestamp: float

@dataclass
class StaffRezeroAuthorized:
    """秤重異常「重新校準歸零」，店員已刷工作證確認。

    放棄待比對的那一筆，把目前重量當成新的穩定基準。這等於接受籃子裡現在的東西，所以只有店員能做
    （使用者按按鈕只送出協助請求）。店員代碼與前後重量記進警告歷史（ALERT_STAFF_REZERO）。
    """

    staff_id: str
    timestamp: float

@dataclass
class LogoutRequested:
    timestamp: float

@dataclass
class ForceLogoutRequested:
    """店員強制登出（棄車、需要重置）：直接清空；清單裡還有商品就發 CRITICAL。"""

    timestamp: float
    reason: str = ""

@dataclass
class TimeoutTick:
    """目前時間，驅動各種逾時檢查（呼叫端約每秒送一次）。"""

    now: float

CartEvent = Union[
    LoginScanned,
    GateEntryDetected,
    GateExitDetected,
    ItemScanned,
    WeightSampleReceived,
    SensorDisconnected,
    SensorReconnected,
    LockForCheckoutRequested,
    PaymentConfirmed,
    RetryWeightCheckRequested,
    VoidPendingItemRequested,
    UnlockCheckoutRequested,
    StaffRezeroAuthorized,
    LogoutRequested,
    ForceLogoutRequested,
    TimeoutTick,
]

@dataclass
class CartAlert:
    code: str
    severity: str
    message: str
    timestamp: float

@dataclass
class CartSession:
    """這次登入到登出的狀態（畫面、記錄直接讀；邏輯都在 CartStateMachine）。"""

    state: str = STATE_UNAUTHENTICATED
    member_id: Optional[str] = None
    session_started_at: Optional[float] = None
    zone_entered_at: Optional[float] = None
    last_activity_at: Optional[float] = None
    locked_at: Optional[float] = None
    payment_confirmed_at: Optional[float] = None
    sensor_connected: bool = True
    current_weight_g: Optional[float] = None

    # 購物中的穩定重量（偵測掃碼前就發生的重量變化）
    stable_weight_g: Optional[float] = None
    # 重量先變、等掃碼時：變化前的基準與開始時間
    unscanned_baseline_g: Optional[float] = None
    unscanned_change_started_at: Optional[float] = None

    # 正在等秤重比對的那一筆
    pending_item_barcode: Optional[str] = None
    pending_item_mode: Optional[str] = None
    pending_item_expected_delta_g: Optional[float] = None
    pending_item_tolerance_g: Optional[float] = None
    pending_item_baseline_g: Optional[float] = None
    weight_check_started_at: Optional[float] = None

    # 最近 5 筆警告；完整歷史用 CartStateMachine.get_alerts()
    recent_alerts: List[CartAlert] = field(default_factory=list)

class CartStateMachine:
    """process_event() 一次處理一個事件，回傳最新的 CartSession。不是執行緒安全的：
    多條執行緒共用時由呼叫端上鎖（ui/bridge.py）。"""

    def __init__(
        self,
        db: DBManager,
        cart: Optional[CartManager] = None,
        config: Optional[dict] = None,
        max_alert_history: int = 200,
    ):
        self.db = db
        self.cart = cart if cart is not None else CartManager()
        self.cfg = config or {}
        self._session = CartSession()
        self._alerts: "deque[CartAlert]" = deque(maxlen=max_alert_history)
        self._weights: "deque" = deque()     # 最近的 (t, g)，算穩定值用
        self._handlers: Dict[type, Callable] = {
            LoginScanned: self._on_login,
            GateEntryDetected: self._on_gate_entry,
            GateExitDetected: self._on_gate_exit,
            ItemScanned: self._on_item_scanned,
            WeightSampleReceived: self._on_weight_sample,
            SensorDisconnected: self._on_sensor_disconnected,
            SensorReconnected: self._on_sensor_reconnected,
            LockForCheckoutRequested: self._on_lock_requested,
            PaymentConfirmed: self._on_payment_confirmed,
            RetryWeightCheckRequested: self._on_retry_weight_check,
            VoidPendingItemRequested: self._on_void_pending_item,
            UnlockCheckoutRequested: self._on_unlock_checkout,
            StaffRezeroAuthorized: self._on_staff_rezero,
            LogoutRequested: self._on_logout_requested,
            ForceLogoutRequested: self._on_force_logout,
            TimeoutTick: self._on_timeout_tick,
        }

    # ------------------------------------------------------------------
    def get_session(self) -> CartSession:
        return self._session

    def get_alerts(self) -> List[CartAlert]:
        return list(self._alerts)

    # ------------------------------------------------------------------
    def process_event(self, event: CartEvent) -> CartSession:
        handler = self._handlers.get(type(event))
        if handler is None:
            raise TypeError(f"未知的事件型別：{type(event)!r}")
        handler(event)
        return self._session

    # ------------------------------------------------------------------
    def _alert(self, severity: str, code: str, message: str, timestamp: Optional[float] = None) -> None:
        ts = timestamp if timestamp is not None else time.time()
        alert = CartAlert(code=code, severity=severity, message=message, timestamp=ts)
        self._alerts.append(alert)
        self._session.recent_alerts = list(self._alerts)[-5:]
        logger.log(_SEVERITY_LOG_LEVEL[severity], "[%s] %s", code, message)

    def _clear_pending(self) -> None:
        s = self._session
        s.pending_item_barcode = None
        s.pending_item_mode = None
        s.pending_item_expected_delta_g = None
        s.pending_item_tolerance_g = None
        s.pending_item_baseline_g = None
        s.weight_check_started_at = None
        s.unscanned_baseline_g = None
        s.unscanned_change_started_at = None

    # ------------------------------------------------------------------
    # 登入
    # ------------------------------------------------------------------
    def _on_login(self, event: LoginScanned) -> None:
        s = self._session
        if s.state != STATE_UNAUTHENTICATED:
            self._alert(
                SEVERITY_INFO, ALERT_UNEXPECTED_EVENT,
                f"目前狀態是 {s.state}，忽略這次登入掃描（要先登出目前帳號才能切換會員）",
                event.timestamp,
            )
            return

        prefix = self.cfg.get("login_barcode_prefix", "")
        if prefix and not event.member_id.startswith(prefix):
            self._alert(
                SEVERITY_WARNING, ALERT_LOGIN_FORMAT_INVALID,
                f"掃到的條碼 '{event.member_id}' 不是會員碼格式（缺少前綴 '{prefix}'），"
                f"可能誤掃到商品條碼",
                event.timestamp,
            )
            return

        member = self.db.get_member(event.member_id)
        if member is None:
            self._alert(
                SEVERITY_WARNING, ALERT_LOGIN_MEMBER_NOT_FOUND,
                f"會員代碼 {event.member_id} 查無資料",
                event.timestamp,
            )
            return

        self._session = CartSession(
            state=STATE_LOGGED_IN_OUTSIDE_ZONE,
            member_id=event.member_id,
            session_started_at=event.timestamp,
            last_activity_at=event.timestamp,
        )
        self.cart.clear()
        logger.info("會員 %s（%s）登入成功，等待進入管制區", event.member_id, member.name)

    # ------------------------------------------------------------------
    # 管制區進出
    # ------------------------------------------------------------------
    def _on_gate_entry(self, event: GateEntryDetected) -> None:
        s = self._session
        if s.state == STATE_LOGGED_IN_OUTSIDE_ZONE:
            s.state = STATE_SHOPPING
            s.zone_entered_at = event.timestamp
            s.last_activity_at = event.timestamp
        elif s.state == STATE_UNAUTHENTICATED:
            self._alert(
                SEVERITY_CRITICAL, ALERT_ZONE_ENTRY_WITHOUT_LOGIN,
                "偵測到進入管制區，但這台購物車目前沒有人登入——可能有人未登入直接推車進入",
                event.timestamp,
            )
        else:
            self._alert(
                SEVERITY_INFO, ALERT_UNEXPECTED_EVENT,
                f"在狀態 {s.state} 收到進入管制區事件，忽略（已經在管制區內或流程已往後）",
                event.timestamp,
            )

    def _on_gate_exit(self, event: GateExitDetected) -> None:
        s = self._session
        if s.state == STATE_AWAITING_EXIT:
            s.state = STATE_SESSION_CLOSED
            s.last_activity_at = event.timestamp
        elif s.state in _IN_ZONE_UNPAID_STATES or s.state == STATE_LOGGED_IN_OUTSIDE_ZONE:
            self._alert(
                SEVERITY_CRITICAL, ALERT_EXIT_WITHOUT_CHECKOUT,
                f"狀態 {s.state}：尚未完成結帳鎖定/付款就偵測到走出管制區——"
                f"可能防損事件，需要人工/警報硬體介入。狀態機不會自動改變狀態，"
                f"問題必須被上層處理，不能被當作正常結束",
                event.timestamp,
            )
        else:
            self._alert(
                SEVERITY_INFO, ALERT_UNEXPECTED_EVENT,
                f"在狀態 {s.state} 收到走出管制區事件，忽略",
                event.timestamp,
            )

    # ------------------------------------------------------------------
    # 掃碼增減商品
    # ------------------------------------------------------------------
    def _on_item_scanned(self, event: ItemScanned) -> None:
        s = self._session

        if s.state == STATE_LOCKED_FOR_CHECKOUT:
            self._alert(
                SEVERITY_WARNING, ALERT_SCAN_WHILE_LOCKED,
                f"已鎖定結帳，忽略掃碼（條碼 {event.barcode}）",
                event.timestamp,
            )
            return

        if s.state in _AWAITING_WEIGHT_STATES:
            self._alert(
                SEVERITY_INFO, ALERT_UNEXPECTED_EVENT,
                f"上一筆商品（條碼 {s.pending_item_barcode}）秤重比對還沒完成，"
                f"忽略新的掃碼（條碼 {event.barcode}），請稍候再掃",
                event.timestamp,
            )
            return

        if s.state not in (STATE_SHOPPING, STATE_AWAITING_ITEM_SCAN):
            self._alert(
                SEVERITY_INFO, ALERT_UNEXPECTED_EVENT,
                f"狀態 {s.state} 不允許掃碼商品，忽略（條碼 {event.barcode}）",
                event.timestamp,
            )
            return

        if not s.sensor_connected or s.current_weight_g is None:
            self._alert(
                SEVERITY_CRITICAL if not s.sensor_connected else SEVERITY_WARNING,
                ALERT_SENSOR_DISCONNECTED,
                "秤重感測器目前無資料，無法驗證這次掃碼，請確認連線後重新掃描"
                f"（條碼 {event.barcode}）",
                event.timestamp,
            )
            return

        # 判斷加入還是移除：
        #   - STATE_SHOPPING：標準流程，先掃碼、重量還沒變 -> 一律當「加入」，
        #     基準值就是掃碼當下的目前重量。
        #   - STATE_AWAITING_ITEM_SCAN：重量在掃碼前就已經變化過，用當時記錄
        #     的方向（比 unscanned_baseline_g 增加還是減少）決定，基準值也要
        #     用『變化前』那個 unscanned_baseline_g，不是掃碼當下的目前重量
        #     （目前重量已經是變化後的數字了）。
        if s.state == STATE_AWAITING_ITEM_SCAN:
            baseline = s.unscanned_baseline_g
            mode = ITEM_SCAN_MODE_REMOVE if s.current_weight_g < baseline else ITEM_SCAN_MODE_ADD
            s.unscanned_baseline_g = None
            s.unscanned_change_started_at = None
        else:
            # 用穩定的基準（不是掃碼當下那一筆，那一筆可能剛好在晃）
            baseline = s.stable_weight_g if s.stable_weight_g is not None else s.current_weight_g
            mode = ITEM_SCAN_MODE_ADD

        if mode == ITEM_SCAN_MODE_ADD:
            product = self.db.get_product(event.barcode)
            if product is None:
                self._alert(
                    SEVERITY_WARNING, ALERT_UNKNOWN_BARCODE,
                    f"掃到的條碼 {event.barcode} 查無商品資料",
                    event.timestamp,
                )
                return
            expected_delta = product.standard_weight_g
            tolerance = product.weight_tolerance_g
            next_state = STATE_AWAITING_WEIGHT_INCREASE
        else:
            if not self.cart.has_item(event.barcode):
                self._alert(
                    SEVERITY_WARNING, ALERT_ITEM_NOT_IN_CART,
                    f"偵測到重量減少後掃到條碼 {event.barcode}，但購物清單裡沒有這項商品，"
                    f"無法判定為移除（可能拿錯了商品，或掃到別的東西）",
                    event.timestamp,
                )
                return
            product = self.db.get_product(event.barcode)
            if product is None:
                # 理論上不該發生：清單裡的東西一定是查過資料庫才加進去的，
                # 這裡查不到代表資料庫資料在購物過程中被改動過，防禦性處理。
                self._alert(
                    SEVERITY_WARNING, ALERT_UNKNOWN_BARCODE,
                    f"購物清單裡的條碼 {event.barcode} 在資料庫查無資料，資料可能不一致",
                    event.timestamp,
                )
                return
            expected_delta = -product.standard_weight_g
            tolerance = product.weight_tolerance_g
            next_state = STATE_AWAITING_WEIGHT_DECREASE

        s.state = next_state
        s.pending_item_barcode = event.barcode
        s.pending_item_mode = mode
        s.pending_item_expected_delta_g = expected_delta
        s.pending_item_tolerance_g = tolerance
        s.pending_item_baseline_g = baseline
        s.weight_check_started_at = event.timestamp
        s.last_activity_at = event.timestamp

    # ------------------------------------------------------------------
    # 秤重比對
    # ------------------------------------------------------------------
    def _settled(self, event: WeightSampleReceived) -> Optional[float]:
        """把這一筆放進視窗，回傳穩定值 w̃；還沒穩回傳 None（定義見檔案開頭）。"""
        window = float(self.cfg.get("settle_window_sec", 0.0) or 0.0)
        if window <= 0:
            return event.grams
        band = float(self.cfg.get("settle_band_g", 2.0))
        w = self._weights
        w.append((event.timestamp, event.grams))
        while w and w[0][0] <= event.timestamp - window:
            w.popleft()
        if len(w) < 3:
            return None
        # 視窗前緣之前那一筆已經被丟掉，所以用「最舊一筆距今多久」看涵蓋了多少時間
        if event.timestamp - w[0][0] < 0.8 * window:
            return None
        vals = [g for _, g in w]
        if max(vals) - min(vals) > band:
            return None
        return statistics.median(vals)

    def _on_weight_sample(self, event: WeightSampleReceived) -> None:
        s = self._session
        s.current_weight_g = event.grams
        settled = self._settled(event)
        if settled is None:
            return          # 還在晃：只更新目前重量，不做任何判斷

        if s.state == STATE_SHOPPING:
            self._track_unscanned_drift(settled, event.timestamp)
            return

        if s.state == STATE_AWAITING_ITEM_SCAN:
            self._check_unscanned_drift_resolved(settled, event.timestamp)
            return

        if s.state not in _AWAITING_WEIGHT_STATES:
            # 其他狀態（鎖定中、已經在秤重異常鎖定等）只更新目前重量，不做
            # 比對判斷。
            return

        baseline = s.pending_item_baseline_g
        expected = s.pending_item_expected_delta_g
        tolerance = s.pending_item_tolerance_g
        if baseline is None or expected is None or tolerance is None:
            # 理論上跟 state 一定同步出現/消失，防禦性檢查避免 None 運算炸掉。
            logger.error("狀態是 %s 但 pending_item 欄位不完整，忽略這筆秤重樣本", s.state)
            return

        delta = settled - baseline

        if abs(delta - expected) <= tolerance:
            self._commit_pending_item()
            self._clear_pending()
            s.state = STATE_SHOPPING
            s.stable_weight_g = settled
            s.last_activity_at = event.timestamp
            return

        same_sign = (delta >= 0) == (expected >= 0)
        if not same_sign and abs(delta) > tolerance:
            self._alert(
                SEVERITY_CRITICAL, ALERT_WEIGHT_DIRECTION_MISMATCH,
                f"條碼 {s.pending_item_barcode}：預期重量變化 {expected:+.1f}g，"
                f"實測卻是 {delta:+.1f}g（方向相反），可能有其他商品同時被拿動",
                event.timestamp,
            )
            s.state = STATE_WEIGHT_MISMATCH_ERROR
            return

        if same_sign and abs(delta) > abs(expected) + tolerance:
            self._alert(
                SEVERITY_WARNING, ALERT_WEIGHT_MISMATCH,
                f"條碼 {s.pending_item_barcode}：預期重量變化 {expected:+.1f}±{tolerance:.1f}g，"
                f"實測 {delta:+.1f}g，超出容差上限（可能拿了不只一件，或拿錯商品）",
                event.timestamp,
            )
            s.state = STATE_WEIGHT_MISMATCH_ERROR
            return

        # 方向對、量還沒超標，視為還在放/拿的過程中，繼續等（會由 TimeoutTick 顧逾時）

    def _commit_pending_item(self) -> None:
        s = self._session
        product = self.db.get_product(s.pending_item_barcode)
        if product is None:
            logger.error("秤重比對成功但條碼 %s 已經查不到商品資料，無法寫入購物清單", s.pending_item_barcode)
            return
        if s.pending_item_mode == ITEM_SCAN_MODE_ADD:
            self.cart.add_item(product)
        else:
            self.cart.remove_item(product.barcode)

    # ------------------------------------------------------------------
    # 掃碼前就發生的重量變化（先拿取/放入，還沒掃碼）
    # ------------------------------------------------------------------
    def _track_unscanned_drift(self, grams: float, timestamp: float) -> None:
        """STATE_SHOPPING 底下持續呼叫（只用穩定值），偵測『重量已經變了但還沒掃碼』。"""
        s = self._session
        threshold = self.cfg.get("unscanned_change_threshold_g", 20.0)

        if s.stable_weight_g is None:
            s.stable_weight_g = grams
            return

        delta = grams - s.stable_weight_g
        if abs(delta) < threshold:
            # 雜訊範圍內，順便讓穩定值跟上小幅漂移，避免長期累積誤判。
            s.stable_weight_g = grams
            return

        s.state = STATE_AWAITING_ITEM_SCAN
        s.unscanned_baseline_g = s.stable_weight_g
        s.unscanned_change_started_at = timestamp
        self._alert(
            SEVERITY_WARNING, ALERT_UNSCANNED_WEIGHT_CHANGE,
            f"偵測到重量{'增加' if delta > 0 else '減少'} {abs(delta):.1f}g，"
            f"但尚未掃描條碼，請掃描剛才拿取／放入的商品",
            timestamp,
        )

    def _check_unscanned_drift_resolved(self, grams: float, timestamp: float) -> None:
        """STATE_AWAITING_ITEM_SCAN 底下持續呼叫：重量如果自己又回到變化前
        的基準值附近（例如手滑碰到籃子），視為虛驚一場，自動回到 SHOPPING，
        不需要使用者做任何操作；否則就只是更新目前重量，繼續等掃碼或逾時
        （逾時由 _on_timeout_tick 處理）。
        """
        s = self._session
        threshold = self.cfg.get("unscanned_change_threshold_g", 20.0)
        baseline = s.unscanned_baseline_g
        if baseline is not None and abs(grams - baseline) < threshold:
            self._alert(
                SEVERITY_INFO, ALERT_UNSCANNED_WEIGHT_AUTO_RESOLVED,
                "先前偵測到的重量變化已經恢復到原本的基準值附近，視為虛驚一場，自動解除",
                timestamp,
            )
            s.unscanned_baseline_g = None
            s.unscanned_change_started_at = None
            s.stable_weight_g = grams
            s.state = STATE_SHOPPING

    # ------------------------------------------------------------------
    # 秤重異常的重試/取消
    # ------------------------------------------------------------------
    def _on_retry_weight_check(self, event: RetryWeightCheckRequested) -> None:
        s = self._session
        if s.state != STATE_WEIGHT_MISMATCH_ERROR:
            self._alert(
                SEVERITY_INFO, ALERT_UNEXPECTED_EVENT,
                f"狀態 {s.state} 不是秤重異常，忽略重試請求",
                event.timestamp,
            )
            return
        if s.current_weight_g is None:
            self._alert(
                SEVERITY_WARNING, ALERT_SENSOR_DISCONNECTED,
                "秤重資料還沒恢復，無法重新比對",
                event.timestamp,
            )
            return
        if s.pending_item_barcode is None:
            # 「只做一半」逾時進來的異常：從頭到尾都還沒掃過碼，沒有
            # pending_item_mode/baseline 可以比對，重試等於重新開始等使用者
            # 掃剛才拿取／放入的那件商品——回到 STATE_AWAITING_ITEM_SCAN，
            # 用現在的重量當新的基準值重新計時。
            s.unscanned_baseline_g = s.current_weight_g
            s.unscanned_change_started_at = event.timestamp
            s.state = STATE_AWAITING_ITEM_SCAN
            s.last_activity_at = event.timestamp
            return
        s.pending_item_baseline_g = s.current_weight_g
        s.weight_check_started_at = event.timestamp
        s.state = (
            STATE_AWAITING_WEIGHT_INCREASE
            if s.pending_item_mode == ITEM_SCAN_MODE_ADD
            else STATE_AWAITING_WEIGHT_DECREASE
        )
        s.last_activity_at = event.timestamp

    def _on_void_pending_item(self, event: VoidPendingItemRequested) -> None:
        s = self._session
        if s.state != STATE_WEIGHT_MISMATCH_ERROR:
            self._alert(
                SEVERITY_INFO, ALERT_UNEXPECTED_EVENT,
                f"狀態 {s.state} 不是秤重異常，忽略取消請求",
                event.timestamp,
            )
            return
        # 「只做一半」逾時進來的異常沒有 pending item 可以放棄，只是把目前
        # 重量重新當成新的穩定基準值，當作這次的重量變化已經人工確認過了。
        never_scanned = s.pending_item_barcode is None
        self._clear_pending()
        if never_scanned:
            s.stable_weight_g = s.current_weight_g
        s.state = STATE_SHOPPING
        s.last_activity_at = event.timestamp

    def _on_staff_rezero(self, event: StaffRezeroAuthorized) -> None:
        s = self._session
        if s.state != STATE_WEIGHT_MISMATCH_ERROR:
            self._alert(
                SEVERITY_INFO, ALERT_UNEXPECTED_EVENT,
                f"狀態 {s.state} 不是秤重異常，忽略重新校準歸零",
                event.timestamp,
            )
            return
        if s.current_weight_g is None or not s.sensor_connected:
            self._alert(
                SEVERITY_WARNING, ALERT_SENSOR_DISCONNECTED,
                "秤重資料還沒恢復，無法重新校準歸零",
                event.timestamp,
            )
            return
        old = s.stable_weight_g
        pending = s.pending_item_barcode
        self._clear_pending()
        s.stable_weight_g = s.current_weight_g
        s.state = STATE_SHOPPING
        s.last_activity_at = event.timestamp
        old_text = "（無）" if old is None else f"{old:.1f}g"
        diff_text = "" if old is None else f"，差 {s.current_weight_g - old:+.1f}g"
        self._alert(
            SEVERITY_INFO, ALERT_STAFF_REZERO,
            f"店員 {event.staff_id} 確認後重新校準歸零：基準 {old_text} → {s.current_weight_g:.1f}g{diff_text}"
            + (f"；待比對的條碼 {pending} 不加入也不移除" if pending else ""),
            event.timestamp,
        )

    # ------------------------------------------------------------------
    # 感測器斷線/恢復
    # ------------------------------------------------------------------
    def _on_sensor_disconnected(self, event: SensorDisconnected) -> None:
        s = self._session
        s.sensor_connected = False
        self._alert(
            SEVERITY_CRITICAL, ALERT_SENSOR_DISCONNECTED,
            "秤重/UART 感測器資料中斷",
            event.timestamp,
        )
        if s.state in _WEIGHT_IN_PROGRESS_STATES:
            # 斷線期間不能繼續信任逾時計時（不知道是東西沒放好還是感測器本
            # 身沒資料），直接轉成需要人工處理的異常狀態，等重新連線後用
            # RetryWeightCheckRequested 重新開始比對。這也涵蓋
            # STATE_AWAITING_ITEM_SCAN：掃碼前的重量變化，斷線期間一樣無法
            # 信任後續的比對結果。
            s.state = STATE_WEIGHT_MISMATCH_ERROR

    def _on_sensor_reconnected(self, event: SensorReconnected) -> None:
        s = self._session
        s.sensor_connected = True
        self._alert(
            SEVERITY_INFO, ALERT_SENSOR_RECONNECTED,
            "感測器資料已恢復",
            event.timestamp,
        )

    # ------------------------------------------------------------------
    # 鎖定結帳 / 付款完成
    # ------------------------------------------------------------------
    def _on_lock_requested(self, event: LockForCheckoutRequested) -> None:
        s = self._session
        if s.state == STATE_SHOPPING:
            s.state = STATE_LOCKED_FOR_CHECKOUT
            s.locked_at = event.timestamp
            s.last_activity_at = event.timestamp
        elif s.state in _WEIGHT_IN_PROGRESS_STATES:
            self._alert(
                SEVERITY_WARNING, ALERT_LOCK_REJECTED_PENDING_WEIGHT,
                "尚有商品秤重比對中（或偵測到尚未掃碼的重量變化），"
                "無法鎖定結帳，請先完成掃碼／比對或取消這筆",
                event.timestamp,
            )
        elif s.state == STATE_WEIGHT_MISMATCH_ERROR:
            self._alert(
                SEVERITY_WARNING, ALERT_LOCK_REJECTED_UNRESOLVED_ERROR,
                "尚有未解決的秤重異常，無法鎖定結帳，請先重試或取消該筆商品",
                event.timestamp,
            )
        elif s.state == STATE_LOCKED_FOR_CHECKOUT:
            pass  # 已經鎖定了，忽略重複請求
        else:
            self._alert(
                SEVERITY_INFO, ALERT_UNEXPECTED_EVENT,
                f"狀態 {s.state} 不允許鎖定結帳",
                event.timestamp,
            )

    def _on_unlock_checkout(self, event: UnlockCheckoutRequested) -> None:
        s = self._session
        if s.state != STATE_LOCKED_FOR_CHECKOUT:
            self._alert(
                SEVERITY_INFO, ALERT_UNEXPECTED_EVENT,
                f"狀態 {s.state} 沒有鎖定結帳，忽略解除鎖定",
                event.timestamp,
            )
            return
        s.state = STATE_SHOPPING
        s.locked_at = None
        s.last_activity_at = event.timestamp
        self._alert(
            SEVERITY_INFO, ALERT_CHECKOUT_UNLOCKED,
            "解除結帳鎖定，回到購物中（鎖定期間的重量變化會在下一筆讀數重新核對）",
            event.timestamp,
        )

    def _on_payment_confirmed(self, event: PaymentConfirmed) -> None:
        s = self._session
        if s.state == STATE_LOCKED_FOR_CHECKOUT:
            s.state = STATE_AWAITING_EXIT
            s.payment_confirmed_at = event.timestamp
            s.last_activity_at = event.timestamp
        else:
            self._alert(
                SEVERITY_WARNING, ALERT_UNEXPECTED_EVENT,
                f"狀態 {s.state} 收到付款完成事件，不合理（應該先進入 {STATE_LOCKED_FOR_CHECKOUT}）",
                event.timestamp,
            )

    # ------------------------------------------------------------------
    # 登出
    # ------------------------------------------------------------------
    def _on_logout_requested(self, event: LogoutRequested) -> None:
        s = self._session
        if s.state == STATE_SESSION_CLOSED:
            self._session = CartSession(last_activity_at=event.timestamp)
            self.cart.clear()
        elif s.state == STATE_UNAUTHENTICATED:
            pass  # 沒登入，忽略
        else:
            self._alert(
                SEVERITY_WARNING, ALERT_LOGOUT_REJECTED_SESSION_NOT_CLOSED,
                f"尚未完成結帳/走出管制區（目前狀態 {s.state}），不能直接登出；"
                f"如果需要強制登出，請用工作人員的強制登出功能",
                event.timestamp,
            )

    def _on_force_logout(self, event: ForceLogoutRequested) -> None:
        s = self._session
        if s.state != STATE_SESSION_CLOSED and self.cart.item_count() > 0:
            self._alert(
                SEVERITY_CRITICAL, ALERT_FORCE_LOGOUT_WITH_ITEMS,
                f"強制登出時購物車內還有 {self.cart.item_count()} 件商品尚未結帳確認，"
                f"需要人工核對（原因：{event.reason or '未說明'}）",
                event.timestamp,
            )
        self.cart.clear()
        self._session = CartSession(last_activity_at=event.timestamp)

    # ------------------------------------------------------------------
    # 逾時檢查
    # ------------------------------------------------------------------
    def _on_timeout_tick(self, event: TimeoutTick) -> None:
        s = self._session
        now = event.now

        if s.state in _AWAITING_WEIGHT_STATES:
            limit = self.cfg.get("weight_match_timeout_sec", 5.0)
            if s.weight_check_started_at is not None and (now - s.weight_check_started_at) >= limit:
                self._alert(
                    SEVERITY_WARNING, ALERT_WEIGHT_MATCH_TIMEOUT,
                    f"條碼 {s.pending_item_barcode} 秤重比對逾時（超過 {limit:.1f} 秒沒有比對成功）",
                    now,
                )
                s.state = STATE_WEIGHT_MISMATCH_ERROR

        elif s.state == STATE_AWAITING_ITEM_SCAN:
            limit = self.cfg.get("unscanned_change_timeout_sec", 8.0)
            if s.unscanned_change_started_at is not None and (now - s.unscanned_change_started_at) >= limit:
                self._alert(
                    SEVERITY_CRITICAL, ALERT_UNSCANNED_WEIGHT_TIMEOUT,
                    f"偵測到重量變化已超過 {limit:.1f} 秒仍未掃描條碼，"
                    f"可能拿取／放入商品後忘記掃碼，需要人工核對",
                    now,
                )
                s.state = STATE_WEIGHT_MISMATCH_ERROR

        elif s.state == STATE_LOGGED_IN_OUTSIDE_ZONE:
            limit = self.cfg.get("zone_entry_timeout_sec", 120.0)
            if s.session_started_at is not None and (now - s.session_started_at) >= limit:
                self._alert(
                    SEVERITY_WARNING, ALERT_ZONE_ENTRY_TIMEOUT,
                    f"登入後超過 {limit:.1f} 秒還沒偵測到進入管制區",
                    now,
                )
                # 設成 None 只是拿來當「這個逾時已經警告過一次」的旗標，避免
                # 每次 tick（例如每秒一次）都重複發同一個警告洗版；下一次真
                # 的要重新檢查，只會是進到新的一次登入（重新建構 CartSession）。
                s.session_started_at = None

        elif s.state == STATE_AWAITING_EXIT:
            limit = self.cfg.get("exit_timeout_after_checkout_sec", 180.0)
            if s.payment_confirmed_at is not None and (now - s.payment_confirmed_at) >= limit:
                self._alert(
                    SEVERITY_WARNING, ALERT_EXIT_TIMEOUT_AFTER_CHECKOUT,
                    f"付款完成後超過 {limit:.1f} 秒還沒偵測到走出管制區",
                    now,
                )
                s.payment_confirmed_at = None  # 同上，避免重複發警告

        elif s.state == STATE_SHOPPING:
            limit = self.cfg.get("shopping_idle_timeout_sec", 600.0)
            if s.last_activity_at is not None and (now - s.last_activity_at) >= limit:
                self._alert(
                    SEVERITY_WARNING, ALERT_SHOPPING_IDLE,
                    f"購物中超過 {limit:.1f} 秒沒有任何動作，可能使用者已離開但忘記登出",
                    now,
                )
                # 更新成 now 而不是設 None：持續閒置的話希望每隔一個 timeout
                # 週期再提醒一次，而不是整段購物過程只警告一次就再也不提醒。
                s.last_activity_at = now

# ----------------------------------------------------------------------
def load_state_machine_config(raw: Optional[dict] = None) -> dict:
    """CartStateMachine 的參數：config.json 的 state_machine 區塊加上 weight 區塊的比對參數。"""
    raw = raw if raw is not None else config.load()
    cfg = config.section("state_machine", raw)
    weight_cfg = raw.get("weight", {})
    cfg["weight_match_timeout_sec"] = weight_cfg.get("weight_match_timeout_sec", 5.0)
    cfg["unscanned_change_threshold_g"] = weight_cfg.get("unscanned_change_threshold_g", 20.0)
    cfg["unscanned_change_timeout_sec"] = weight_cfg.get("unscanned_change_timeout_sec", 8.0)
    cfg["settle_window_sec"] = weight_cfg.get("settle_window_sec", 0.0)
    cfg["settle_band_g"] = weight_cfg.get("settle_band_g", 2.0)
    return cfg
