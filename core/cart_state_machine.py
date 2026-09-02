"""
core/cart_state_machine.py

Phase 4：購物流程狀態機——「底層架構的支持」，負責保障整趟購物流程本身的
正確性（掃碼、秤重比對、防損、閘門進出的合理性），不含 LLM 推薦、預算分
析這類進階功能（那些排在 Phase 6 之上，見 README「前身專題功能取捨」章節）。

涵蓋的完整流程（使用者的要求）：
    人進商店拿到推車 -> 登入帳號 -> 進入管制區 -> 購物（商品增減，含掃碼加入
    /移除跟秤重比對）-> 鎖定結帳 -> （付款，付款本身不在這支的範圍內，這裡只
    接收「付款完成」這個結果事件）-> 走出管制區 -> 登出帳號。

設計原則：這條路上每一步都可能出錯，出錯的處理方式分兩種——
    (a) 可以直接在軟體層擋下來/引導使用者重試的（例如條碼查無資料、秤重沒
        對上），狀態機負責擋下錯誤動作、給出明確的 CartAlert，並提供合理的
        重試/取消路徑（RetryWeightCheckRequested / VoidPendingItemRequested）。
    (b) 沒辦法只靠軟體解決、代表可能有防損疑慮的（例如結帳前就走出管制區），
        狀態機不會假裝沒事發生、也不會自己幫使用者硬轉成「正常結束」的狀態，
        只會發出最高等級（CRITICAL）的 CartAlert，讓上層（UI、警報硬體、店員
        介入）決定怎麼處理——狀態機本身沒有能力真的攔住一個人，這是軟體邊界，
        不是漏做。

跟目前硬體現況的對應關係（決定這支怎麼設計的關鍵背景）：
    - 登入：目前硬體只有 USB 條碼掃描器，所以登入方式是「掃會員條碼/QR
      code」（不是密碼、不是 RFID 卡）。
    - 「進入/走出管制區」：目前沒有閘門/RFID gate 這類專用硬體，這支先假設
      未來會有這樣的硬體、以事件（GateEntryDetected / GateExitDetected）的
      形式餵進來；實際硬體介面確定後，寫一個 drivers/gate_sensor.py 之類的
      模組把硬體訊號轉成這兩個事件即可，這支狀態機的邏輯不用改。
    - 「鎖定結帳」：目前購物車沒有電子鎖/煞車這類實體機構，所以「鎖定」純粹
      是軟體狀態鎖——LOCKED_FOR_CHECKOUT 狀態下 UI 應該要禁止繼續操作增減
      商品的功能，僅此而已，不代表車子被真的鎖住了。
    - 「同一會員是否已經在別台購物車登入」：單機沒辦法知道其他購物車的狀
      態，這件事需要中央伺服器/共用資料庫才能做，目前架構沒有這塊，所以這
      支不處理跨購物車重複登入偵測（見 database/db_manager.py 開頭的說明）。

跟秤重比對相關的核心邏輯（AWAITING_WEIGHT_INCREASE / _DECREASE 這兩個狀態）：
    掃碼當下記錄「目前重量」當基準值（pending_item_baseline_g），之後每收到
    一筆新的秤重讀數（WeightSampleReceived，換算好的公克數，換算公式見
    core/weight_convert.py），就算跟基準值的差 delta，拿去跟這個商品在
    database 裡登記的 standard_weight_g（預期變化量，加入是正、移除是負）±
    weight_tolerance_g（每個商品自己的容差，見 database/db_manager.py 的
    Product schema）比對：
        - 差在容差範圍內 -> 比對成功，商品正式加入/移出購物清單
          （CartManager.add_item/remove_item），回到 SHOPPING。
        - 方向反過來（該增加卻減少，或反之）且超出容差 -> 直接判定
          WEIGHT_DIRECTION_MISMATCH（可能有其他商品同時被拿動），不用等逾時。
        - 方向對，但差值已經超出容差上限（可能拿了不只一件、或拿錯商品）
          -> WEIGHT_MISMATCH，一樣不用等逾時。
        - 還在容差範圍外但方向對、量也還沒超標 -> 視為「還在放/拿的過程
          中」，繼續等，直到比對成功或等到逾時
          （config.json 的 weight.weight_match_timeout_sec）。
    這個模型完全可以用假的 WeightSampleReceived 事件做離線邏輯測試，不需要
    真的秤重硬體（跟 core/odometry_engine.py 的測試哲學一致）。

用法（本機互動模擬整套流程，不需要真實硬體）：
    python3 -m core.cart_state_machine --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from database.db_manager import DBManager
from core.cart_manager import CartManager

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


# ----------------------------------------------------------------------
# 狀態常數（沿用專案既有風格：用字串常數而不是 enum.Enum，
# 跟 core/position_types.py 的 POSITION_SOURCE_*/YAW_SOURCE_* 一致）
# ----------------------------------------------------------------------
STATE_UNAUTHENTICATED = "unauthenticated"
STATE_LOGGED_IN_OUTSIDE_ZONE = "logged_in_outside_zone"
STATE_SHOPPING = "shopping"
STATE_AWAITING_WEIGHT_INCREASE = "awaiting_weight_increase"
STATE_AWAITING_WEIGHT_DECREASE = "awaiting_weight_decrease"
STATE_WEIGHT_MISMATCH_ERROR = "weight_mismatch_error"
STATE_LOCKED_FOR_CHECKOUT = "locked_for_checkout"
STATE_AWAITING_EXIT = "awaiting_exit"
STATE_SESSION_CLOSED = "session_closed"

_AWAITING_WEIGHT_STATES = (STATE_AWAITING_WEIGHT_INCREASE, STATE_AWAITING_WEIGHT_DECREASE)

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
# 警告代碼——整趟流程每個「可能出問題」的地方各對應一個代碼，方便 UI/log/
# 之後的異常偵測模型（Phase 6 anomaly_detector.py）依代碼分類統計，不用
# 去解析訊息文字。
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


# ----------------------------------------------------------------------
# 事件——每一種外部輸入各自一個 dataclass（不是共用一個 type+payload 的通用
# event），好處是型別檢查跟可讀性都比較好，跟專案裡 UartPacket/PositionEstimate
# 的風格一致。timestamp 由呼叫方（真實系統整合時）在事件真正發生的當下填入，
# 不是 handle_event() 呼叫的當下——這樣事件就算被排隊延後處理，時間點還是準的。
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
    barcode: str
    mode: str  # ITEM_SCAN_MODE_ADD / ITEM_SCAN_MODE_REMOVE，由 UI 的「加入/移除」
    # 模式切換決定，這支狀態機本身不判斷使用者想加入還是移除。
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
    """付款流程本身不在這支的範圍內（沒有金流/收銀邏輯），這裡只接收「付款
    已完成」這個結果，交由上層（結帳 UI/金流模組）在確認付款後送出這個事件。
    """

    timestamp: float


@dataclass
class RetryWeightCheckRequested:
    """WEIGHT_MISMATCH_ERROR 狀態下，使用者/店員確認要重新秤一次（例如剛剛
    手滑碰到秤台，現在東西已經放好了），用目前的重量重新當基準值再等一次。
    """

    timestamp: float


@dataclass
class VoidPendingItemRequested:
    """WEIGHT_MISMATCH_ERROR 狀態下，放棄這一筆掃碼（商品不加入/不移除），
    回到 SHOPPING，不影響清單裡其他已經確認過的商品。
    """

    timestamp: float


@dataclass
class LogoutRequested:
    timestamp: float


@dataclass
class ForceLogoutRequested:
    """店員協助的強制登出（例如使用者棄車離開、系統卡住需要重置），不檢查
    是否已經正常結帳/走出管制區，直接清空狀態——但如果清單裡還有商品，會發
    出 CRITICAL 警告，因為這代表可能有商品沒有被正確結帳。
    """

    timestamp: float
    reason: str = ""


@dataclass
class TimeoutTick:
    """驅動各種逾時檢查用的『目前時間』事件。不需要幫每個逾時各自開一條計時
    執行緒，只要主迴圈固定間隔（例如每秒一次）丟一個這個事件進來，狀態機自
    己比對經過時間，超過門檻就發警告。這個設計選擇是為了讓逾時邏輯也能用
    純邏輯測試（直接建構帶特定 now 值的 TimeoutTick，不用真的等待）。
    """

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
    """目前這次登入~登出的狀態快照。跟 core/position_types.py 的
    PositionEstimate 一樣，這是設計給下游（UI、店員介面、log）直接讀的穩定
    格式，不含任何邏輯——所有邏輯都在 CartStateMachine 裡。
    """

    state: str = STATE_UNAUTHENTICATED
    member_id: Optional[str] = None
    session_started_at: Optional[float] = None
    zone_entered_at: Optional[float] = None
    last_activity_at: Optional[float] = None
    locked_at: Optional[float] = None
    payment_confirmed_at: Optional[float] = None
    sensor_connected: bool = True
    current_weight_g: Optional[float] = None

    # 目前正在等待秤重比對的那一筆掃碼（沒有在等待時全部是 None）
    pending_item_barcode: Optional[str] = None
    pending_item_mode: Optional[str] = None
    pending_item_expected_delta_g: Optional[float] = None
    pending_item_tolerance_g: Optional[float] = None
    pending_item_baseline_g: Optional[float] = None
    weight_check_started_at: Optional[float] = None

    # 最近幾筆警告，方便 UI 直接顯示；完整歷史用 CartStateMachine.get_alerts()
    recent_alerts: List[CartAlert] = field(default_factory=list)


class CartStateMachine:
    """整套購物流程的狀態機。process_event() 本身是純邏輯（除了呼叫
    DBManager 查資料庫以外沒有其他 I/O、沒有背景執行緒），方便用假資料/假
    DB 單元測試；真實系統整合時由主迴圈把各個 driver（barcode_scanner、
    uart_receiver 換算出的重量、之後的 gate_sensor）產生的事件餵進來。
    """

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
        self._max_alert_history = max_alert_history
        self._session = CartSession()
        self._alerts: List[CartAlert] = []

    # ------------------------------------------------------------------
    def get_session(self) -> CartSession:
        return self._session

    def get_alerts(self) -> List[CartAlert]:
        return list(self._alerts)

    # ------------------------------------------------------------------
    def process_event(self, event: CartEvent) -> CartSession:
        if isinstance(event, LoginScanned):
            self._on_login(event)
        elif isinstance(event, GateEntryDetected):
            self._on_gate_entry(event)
        elif isinstance(event, GateExitDetected):
            self._on_gate_exit(event)
        elif isinstance(event, ItemScanned):
            self._on_item_scanned(event)
        elif isinstance(event, WeightSampleReceived):
            self._on_weight_sample(event)
        elif isinstance(event, SensorDisconnected):
            self._on_sensor_disconnected(event)
        elif isinstance(event, SensorReconnected):
            self._on_sensor_reconnected(event)
        elif isinstance(event, LockForCheckoutRequested):
            self._on_lock_requested(event)
        elif isinstance(event, PaymentConfirmed):
            self._on_payment_confirmed(event)
        elif isinstance(event, RetryWeightCheckRequested):
            self._on_retry_weight_check(event)
        elif isinstance(event, VoidPendingItemRequested):
            self._on_void_pending_item(event)
        elif isinstance(event, LogoutRequested):
            self._on_logout_requested(event)
        elif isinstance(event, ForceLogoutRequested):
            self._on_force_logout(event)
        elif isinstance(event, TimeoutTick):
            self._on_timeout_tick(event)
        else:
            raise TypeError(f"未知的事件型別：{type(event)!r}")
        return self._session

    # ------------------------------------------------------------------
    def _alert(self, severity: str, code: str, message: str, timestamp: Optional[float] = None) -> None:
        ts = timestamp if timestamp is not None else time.time()
        alert = CartAlert(code=code, severity=severity, message=message, timestamp=ts)
        self._alerts.append(alert)
        if len(self._alerts) > self._max_alert_history:
            self._alerts.pop(0)
        self._session.recent_alerts = self._alerts[-5:]
        logger.log(_SEVERITY_LOG_LEVEL[severity], "[%s] %s", code, message)

    def _clear_pending(self) -> None:
        s = self._session
        s.pending_item_barcode = None
        s.pending_item_mode = None
        s.pending_item_expected_delta_g = None
        s.pending_item_tolerance_g = None
        s.pending_item_baseline_g = None
        s.weight_check_started_at = None

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
        elif s.state in (
            STATE_LOGGED_IN_OUTSIDE_ZONE,
            STATE_SHOPPING,
            STATE_AWAITING_WEIGHT_INCREASE,
            STATE_AWAITING_WEIGHT_DECREASE,
            STATE_WEIGHT_MISMATCH_ERROR,
            STATE_LOCKED_FOR_CHECKOUT,
        ):
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

        if s.state != STATE_SHOPPING:
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

        if event.mode == ITEM_SCAN_MODE_ADD:
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
        elif event.mode == ITEM_SCAN_MODE_REMOVE:
            if not self.cart.has_item(event.barcode):
                self._alert(
                    SEVERITY_WARNING, ALERT_ITEM_NOT_IN_CART,
                    f"購物清單裡沒有條碼 {event.barcode}，無法移除",
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
        else:
            raise ValueError(f"ItemScanned.mode 必須是 '{ITEM_SCAN_MODE_ADD}' 或 '{ITEM_SCAN_MODE_REMOVE}'，收到 {event.mode!r}")

        s.state = next_state
        s.pending_item_barcode = event.barcode
        s.pending_item_mode = event.mode
        s.pending_item_expected_delta_g = expected_delta
        s.pending_item_tolerance_g = tolerance
        s.pending_item_baseline_g = s.current_weight_g
        s.weight_check_started_at = event.timestamp
        s.last_activity_at = event.timestamp

    # ------------------------------------------------------------------
    # 秤重比對
    # ------------------------------------------------------------------
    def _on_weight_sample(self, event: WeightSampleReceived) -> None:
        s = self._session
        s.current_weight_g = event.grams

        if s.state not in _AWAITING_WEIGHT_STATES:
            # 平常購物中（SHOPPING）或其他狀態，只更新目前重量供下次掃碼當
            # 基準值用，不做比對判斷。
            return

        baseline = s.pending_item_baseline_g
        expected = s.pending_item_expected_delta_g
        tolerance = s.pending_item_tolerance_g
        if baseline is None or expected is None or tolerance is None:
            # 理論上跟 state 一定同步出現/消失，防禦性檢查避免 None 運算炸掉。
            logger.error("狀態是 %s 但 pending_item 欄位不完整，忽略這筆秤重樣本", s.state)
            return

        delta = event.grams - baseline

        if abs(delta - expected) <= tolerance:
            self._commit_pending_item()
            self._clear_pending()
            s.state = STATE_SHOPPING
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
        self._clear_pending()
        s.state = STATE_SHOPPING
        s.last_activity_at = event.timestamp

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
        if s.state in _AWAITING_WEIGHT_STATES:
            # 斷線期間不能繼續信任逾時計時（不知道是東西沒放好還是感測器本
            # 身沒資料），直接轉成需要人工處理的異常狀態，等重新連線後用
            # RetryWeightCheckRequested 重新開始比對。
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
        elif s.state in _AWAITING_WEIGHT_STATES:
            self._alert(
                SEVERITY_WARNING, ALERT_LOCK_REJECTED_PENDING_WEIGHT,
                "尚有商品秤重比對中，無法鎖定結帳，請先完成或取消這筆",
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
def load_state_machine_config() -> dict:
    """讀 config.json，把 CartStateMachine 需要的參數合併成一份扁平的 dict
    （config.json 裡分散在 "weight" 跟 "state_machine"兩個區塊，是因為
    weight_match_timeout_sec 語意上更接近「秤重」設定、其他逾時參數是狀態機
    本身的設定，但 CartStateMachine.cfg 用起來不需要分層，所以這裡合併）。
    """
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    cfg = dict(raw.get("state_machine", {}))
    cfg["weight_match_timeout_sec"] = raw.get("weight", {}).get("weight_match_timeout_sec", 5.0)
    # 拿掉純註解欄位（前綴底線），避免混進來
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


# ----------------------------------------------------------------------
# 互動模擬 CLI——閘門硬體、真實掃描器整合都還沒做，這裡讓使用者不需要任何
# 硬體就能手動走一次「登入->進管制區->掃碼->秤重->鎖定->付款->出管制區->
# 登出」的完整流程，驗證狀態機邏輯跟訊息是否合理（跟專案裡其他校正/驗證工
# 具一樣的「先用假資料把流程跑通」哲學）。
# ----------------------------------------------------------------------
def _print_session(sm: "CartStateMachine") -> None:
    s = sm.get_session()
    print(f"\n--- 目前狀態：{s.state} ---")
    print(f"會員：{s.member_id}　目前重量：{s.current_weight_g}　感測器連線：{s.sensor_connected}")
    if s.pending_item_barcode:
        print(
            f"等待比對中：條碼={s.pending_item_barcode} 模式={s.pending_item_mode} "
            f"預期變化={s.pending_item_expected_delta_g:+.1f}±{s.pending_item_tolerance_g:.1f}g "
            f"基準={s.pending_item_baseline_g}"
        )
    items = sm.cart.list_items()
    if items:
        print("購物清單：")
        for it in items:
            print(f"  {it.name} x{it.quantity}　${it.subtotal:.0f}")
        print(f"總計：${sm.cart.total_price():.0f}")
    if s.recent_alerts:
        print("最近警告：")
        for a in s.recent_alerts[-3:]:
            print(f"  [{a.severity}] {a.code}: {a.message}")


def _run_simulation(db_path: Optional[str]) -> None:
    db = DBManager(db_path)
    db.init_db(seed=True)
    cfg = load_state_machine_config()
    sm = CartStateMachine(db=db, config=cfg)

    sim_now = time.time()

    menu = """
可用指令：
  1  登入（輸入會員代碼，測試資料：MEMBER-0001 / MEMBER-0002）
  2  進入管制區
  3  掃商品條碼 - 加入（測試資料例如 4710018001234）
  4  掃商品條碼 - 移除
  5  回報秤重讀數（輸入目前公克數，模擬 HX711 換算後的值）
  6  快轉時間（檢查逾時，輸入要快轉幾秒）
  7  鎖定結帳
  8  確認付款完成
  9  走出管制區
 10  登出
 11  強制登出（工作人員，需輸入原因）
 12  感測器斷線
 13  感測器恢復
  0  結束
"""
    print("=== Phase 4 購物流程狀態機互動模擬（不需要真實硬體）===")
    print(menu)
    _print_session(sm)

    while True:
        choice = input("\n輸入指令代號：").strip()
        if choice == "0":
            break
        elif choice == "1":
            member_id = input("會員代碼：").strip()
            sm.process_event(LoginScanned(member_id=member_id, timestamp=sim_now))
        elif choice == "2":
            sm.process_event(GateEntryDetected(timestamp=sim_now))
        elif choice == "3":
            barcode = input("條碼：").strip()
            sm.process_event(ItemScanned(barcode=barcode, mode=ITEM_SCAN_MODE_ADD, timestamp=sim_now))
        elif choice == "4":
            barcode = input("條碼：").strip()
            sm.process_event(ItemScanned(barcode=barcode, mode=ITEM_SCAN_MODE_REMOVE, timestamp=sim_now))
        elif choice == "5":
            grams = float(input("目前公克數：").strip())
            sm.process_event(WeightSampleReceived(grams=grams, timestamp=sim_now))
        elif choice == "6":
            delta = float(input("快轉幾秒：").strip())
            sim_now += delta
            sm.process_event(TimeoutTick(now=sim_now))
        elif choice == "7":
            sm.process_event(LockForCheckoutRequested(timestamp=sim_now))
        elif choice == "8":
            sm.process_event(PaymentConfirmed(timestamp=sim_now))
        elif choice == "9":
            sm.process_event(GateExitDetected(timestamp=sim_now))
        elif choice == "10":
            sm.process_event(LogoutRequested(timestamp=sim_now))
        elif choice == "11":
            reason = input("原因：").strip()
            sm.process_event(ForceLogoutRequested(timestamp=sim_now, reason=reason))
        elif choice == "12":
            sm.process_event(SensorDisconnected(timestamp=sim_now))
        elif choice == "13":
            sm.process_event(SensorReconnected(timestamp=sim_now))
        else:
            print(menu)
            continue

        if sm.get_session().state == STATE_WEIGHT_MISMATCH_ERROR:
            print("(目前在秤重異常狀態，輸入 r 重試比對，或輸入 v 放棄這筆商品)")
        _print_session(sm)

        if sm.get_session().state == STATE_WEIGHT_MISMATCH_ERROR:
            follow_up = input("r=重試 / v=放棄 / 直接按 Enter 跳過：").strip().lower()
            if follow_up == "r":
                sm.process_event(RetryWeightCheckRequested(timestamp=sim_now))
                _print_session(sm)
            elif follow_up == "v":
                sm.process_event(VoidPendingItemRequested(timestamp=sim_now))
                _print_session(sm)


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Phase 4 購物流程狀態機")
    parser.add_argument("--simulate", action="store_true", help="啟動互動模擬（不需要真實硬體）")
    parser.add_argument("--db", default=None, help="資料庫路徑，預設 database/inventory.db")
    args = parser.parse_args()

    if args.simulate:
        _run_simulation(args.db)
        return 0

    print("目前只支援 --simulate 互動模擬。真實硬體整合（barcode_scanner/uart_receiver/"
          "之後的 gate_sensor 產生事件餵進 CartStateMachine.process_event()）留給主程式"
          "（main.py，Phase 5）串接。")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
