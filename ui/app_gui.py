"""
ui/app_gui.py

Phase 5：把 13 頁 UI（`ui/templates/index.html`，由 `tools/build_app_ui.py`
從 `design-cart-ui/*.dc.html` 組裝而成）接上 `core.cart_state_machine`
+ `database.db_manager` + `core.cart_manager`，變成一支在 Pi 上執行的觸控應用。

================================================================
單一實體路徑原則（2026-09-15 重構）
================================================================
這支程式**只走真實硬體，沒有任何模擬、退回或旁路**。三個裝置都是啟動時必須
連上的硬性條件，任何一個接不上就直接印出原因並結束（exit 1），不會半殘地開起
來讓人以為系統正常：

    條碼掃描器  drivers/barcode_scanner.py（evdev 獨佔 /dev/input/event*）
    秤重        drivers/uart_receiver.py 的 hx711_raw，經 core/weight_convert 換算
    管制區閘門  drivers/ble_beacon_scanner.py + core/gate_monitor.py（BLE 雙 Beacon 差分）

會這樣訂，是因為之前每個裝置都有兩三種「接不到就退回模擬」的路徑（掃描器有
evdev/鍵盤模式、秤重有展示模式、閘門有開發面板按鈕），結果是：出問題的時候
第一件事不是查硬體，而是要先搞清楚自己現在到底走在哪一條路上，而且畫面會
在硬體其實沒在運作的情況下看起來一切正常。寧可開不起來，也不要假裝正常。

開發測試面板（畫面右上角 DEV）因此改成**唯讀的診斷面板**：顯示目前狀態機狀態、
最近收到的條碼、秤重讀數、BLE 判定次數。沒有任何可以「製造」事件的按鈕。

架構分工：
    - `Bridge`（本檔案）：pywebview 的 js_api 物件，只做「前端呼叫 -> 轉成
      `CartStateMachine` 事件 -> 組出前端要畫的完整畫面狀態（`_state_payload`）
      再丟回去」，不含任何 UI 邏輯（畫面切換、選取樣式都在 index.html 的 JS）。
    - 三個背景執行緒（`_BarcodeScannerConsumer`／`_UartWeightConsumer`／
      `_GateConsumer`）把硬體事件餵進狀態機。這些是背景執行緒單方面發生的，
      沒有對應的前端呼叫可以掛 .then()，所以由 Python 主動呼叫 JS 的
      `APP.render()` 把最新狀態推過去（`Bridge._push_state()`）。
    - 前端未建模的「精靈式引導流程」（歡迎頁 -> 登入 -> 讀取個人化設定 ->
      過敏原/飲食/宗教/預算 四步）不是 `CartStateMachine` 的狀態（那邊只有
      營運層級的狀態），所以用 `self._ui_stage` 另外疊一層。

已知但這一版刻意不假裝做到的缺口（面板/按鈕會直接跳提示，不會靜悄悄地假裝成功）：
    1. `members` 表只有 member_id/name，沒有欄位存過敏原/飲食/預算/常購清單
       ——LoadingProfile 的「情境 A：找到已儲存設定」查不到真資料，選 A 一樣
       會走精靈重問一次；精靈結果只存在這次執行的記憶體裡，關掉就不見。
    2. Checkout 畫面「✕ 返回繼續選購」——狀態機沒有「解除鎖定」的事件。
    3. WeightAlert 的「重新校準歸零」「呼叫店員協助」、ExitConfirm 的
       「取消本次結帳／需要協助」——沒有對應的狀態機事件或店員通知系統。

用法：
    python3 -m ui.app_gui                     # 三個硬體都要接好
    python3 -m ui.app_gui --fullscreen        # 全螢幕（螢幕本身是 720x1280 時建議）
    python3 -m ui.app_gui --db /path/to.db
    python3 -m ui.app_gui --port /dev/ttyAMA0 --baud 115200
    python3 -m ui.app_gui --scanner-hint USBKey      # 掃描器裝置名稱關鍵字
    python3 -m ui.app_gui --adapter hci0             # 藍牙介面
    python3 -m ui.app_gui --beacon-timeout 30        # 開機時等 Beacon 現身的秒數

權限：evdev 要讀 /dev/input/event*，需要使用者在 input 群組裡（做一次就好）：
    sudo usermod -aG input $USER     # 重新登入後生效
藍牙如果打不開：sudo rfkill unblock bluetooth

退出：畫面右上角 ✕ 按鈕，或鍵盤 Esc；真的卡住就另開終端機 `pkill -f ui.app_gui`。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import signal
import threading
import time
from pathlib import Path
from typing import List, Optional

# 跟 WebKitGTK 有關：硬體合成在部分顯示環境（尤其遠端 VNC/螢幕分享）下容易
# 花屏，要在 import webview 之前設，晚了沒用。
os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

from core.cart_manager import CartManager
from core.cart_state_machine import (
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
    STATE_WEIGHT_MISMATCH_ERROR,
    TimeoutTick,
    VoidPendingItemRequested,
    WeightSampleReceived,
    load_state_machine_config,
)
from core.gate_monitor import GATE_CROSSING_ENTERING, GateMonitor
from core.landmark_correction import load_landmark_config
from core.weight_convert import raw_to_grams
from database.db_manager import DBManager, Member

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

SCREEN_W = 720
SCREEN_H = 1280

# 精靈各步驟的合法畫面代號。wizard_goto() 只接受這些，避免前端傳錯字串時悄悄
# 把畫面切到一個 index.html 裡根本不存在的 #screen-xxx（那會變成整頁空白，
# 而不是一個看得出來的錯誤）。
_WIZARD_STAGES = ("login", "loading_profile", "allergens", "diet", "religion", "budget")

_ALLERGEN_OPTIONS = (
    "堅果類", "甲殼類", "芒果", "花生", "牛奶／羊奶", "蛋",
    "芝麻", "含麩質穀物", "大豆", "魚類", "亞硫酸鹽", "都沒有",
)
_RELIGION_OPTIONS = ("無特別宗教飲食規範", "伊斯蘭教（清真／Halal）", "印度教", "台灣民俗不食牛")

# 訪客登入用的假會員代碼：要符合 config.json 的 login_barcode_prefix 格式，
# 才過得了 CartStateMachine._on_login() 的條碼格式檢查。
_GUEST_SUFFIX = "GUEST"


class Bridge:
    """pywebview 的 js_api 物件。每個 public 方法對應前端一種
    `APP.callApi(name, ...)` 呼叫；除了 `quit()` 以外都回傳
    `self._state_payload()`，前端收到後直接 `render()`。
    """

    def __init__(self, db: DBManager, sm: CartStateMachine, window_holder: dict):
        self.db = db
        self.sm = sm
        self._window_holder = window_holder
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

    def _current_screen(self) -> str:
        if self._ui_stage is not None:
            return self._ui_stage
        s = self.sm.get_session()
        state = s.state
        if state in (
            STATE_SHOPPING,
            STATE_AWAITING_WEIGHT_INCREASE,
            STATE_AWAITING_WEIGHT_DECREASE,
            STATE_AWAITING_ITEM_SCAN,
            # 精靈走完但還沒推進管制區。畫面停在購物主畫面，提示區會顯示
            # 「等待進入管制區」——這是真實狀態，不是假裝已經開始購物。
            STATE_LOGGED_IN_OUTSIDE_ZONE,
        ):
            return "shopping"
        if state == STATE_WEIGHT_MISMATCH_ERROR:
            return "weight_alert"
        if state == STATE_LOCKED_FOR_CHECKOUT:
            return "checkout"
        if state == STATE_AWAITING_EXIT:
            return "exit_confirm"
        if state == STATE_SESSION_CLOSED:
            # 走出管制區後立刻登出、清空這次工作階段，回到歡迎頁讓下一位使用
            # 者不用等——CartStateMachine 不會自己做這件事（它只負責記錄「這
            # 一階段結束了」，重新開始要外部送 LogoutRequested）。
            self.sm.process_event(LogoutRequested(timestamp=time.time()))
            self._reset_wizard_profile()
            self._ui_stage = "main"
            return "main"
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
        # 順序很重要：_current_screen() 在偵測到 STATE_SESSION_CLOSED 時會順手
        # 觸發登出（清空購物車、重置成全新的 CartSession），所以一定要先呼叫
        # 它把這個「畫面切換的副作用」做完，再去讀 session/購物車快照——不然
        # 畫面欄位已經是新的一輪、但 sm_state/購物車欄位還是登出前的舊資料。
        screen = self._current_screen()
        s = self.sm.get_session()
        items = self.sm.cart.list_items()
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
                {"name": it.name, "quantity": it.quantity, "subtotal": it.subtotal}
                for it in items
            ],
            "cart_count": self.sm.cart.item_count(),
            "cart_total": self.sm.cart.total_price(),
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
            # 唯讀診斷用
            "last_scan": self._last_scan,
            "gate_events": self._gate_events,
        }

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
        """背景執行緒（硬體）觸發的狀態變化，主動推給前端重畫。

        一般操作是「使用者按按鈕 -> callApi() -> .then(render)」一來一回，但
        硬體事件是背景執行緒單方面發生的，沒有對應的前端呼叫可以掛 .then()。
        視窗還沒建立/頁面還沒載入完成時安靜跳過即可，不是致命錯誤。
        """
        window = self._window_holder.get("window")
        if window is None:
            return
        try:
            payload = json.dumps(self._state_payload(), ensure_ascii=False)
            window.evaluate_js(f"window.APP && window.APP.render({payload})")
        except Exception:  # noqa: BLE001 — 背景執行緒，推送失敗不該讓硬體執行緒掛掉
            logger.exception("推送畫面狀態到前端失敗")

    # ------------------------------------------------------------------
    # 畫面狀態查詢
    # ------------------------------------------------------------------
    def get_state(self) -> dict:
        return self._state_payload()

    # ------------------------------------------------------------------
    # 歡迎頁 / 登入
    # ------------------------------------------------------------------
    def go_to_login(self) -> dict:
        self._last_error = None
        self._ui_stage = "login"
        return self._state_payload()

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
        self._ui_stage = "loading_profile"
        return self._state_payload()

    def continue_as_guest(self) -> dict:
        self._last_error = None
        self.sm.process_event(LoginScanned(member_id=self._guest_member_id, timestamp=time.time()))
        self._ui_stage = "loading_profile"
        return self._state_payload()

    def loading_profile_choice(self, choice: str) -> dict:
        self._reset_wizard_profile()
        if choice == "A":
            self._last_error = (
                "目前資料庫還沒有欄位可以存過敏原／飲食習慣／預算等個人化設定"
                "（members 資料表只有代碼跟姓名），所以找不到「已儲存的設定」，"
                "先照精靈重新設定一次——這次設定一樣只會留在這次購物，不會存起來"
            )
        else:
            self._last_error = None
        self._ui_stage = "allergens"
        return self._state_payload()

    # ------------------------------------------------------------------
    # 偏好精靈
    # ------------------------------------------------------------------
    def wizard_toggle_allergen(self, name: str) -> dict:
        self._last_error = None
        if name not in _ALLERGEN_OPTIONS:
            return self._fail(f"不認得的過敏原選項：{name}")
        if name in self._wizard_allergens:
            self._wizard_allergens.remove(name)
        else:
            self._wizard_allergens.append(name)
        return self._state_payload()

    def wizard_toggle_religion(self, name: str) -> dict:
        self._last_error = None
        if name not in _RELIGION_OPTIONS:
            return self._fail(f"不認得的宗教飲食選項：{name}")
        if name in self._wizard_religion:
            self._wizard_religion.remove(name)
        else:
            self._wizard_religion.append(name)
        return self._state_payload()

    def wizard_set_diet(self, value: str) -> dict:
        self._last_error = None
        self._wizard_diet = value
        return self._state_payload()

    def wizard_goto(self, stage: str) -> dict:
        if stage not in _WIZARD_STAGES:
            return self._fail(f"不認得的精靈步驟：{stage}")
        self._last_error = None
        self._ui_stage = stage
        return self._state_payload()

    def set_budget(self, value: str) -> dict:
        self._last_error = None
        value = (value or "").strip()
        if value == "unlimited" or value == "":
            self._budget = None
        else:
            try:
                budget = float(value)
            except (TypeError, ValueError):
                return self._fail(f"預算金額看不懂：{value!r}，請輸入數字或選擇「不限」")
            if budget < 0:
                return self._fail("預算不能是負數")
            self._budget = budget
        # 精靈到此結束。**不會**在這裡自己觸發進場——進入管制區是由真的 BLE
        # 雙 Beacon 判定的（見 _GateConsumer），這裡只是把畫面交給購物主畫面，
        # 狀態機仍然停在「已登入、未進管制區」，畫面會顯示等待提示。
        self._ui_stage = None
        return self._state_payload()

    # ------------------------------------------------------------------
    # 結帳 / 秤重異常
    # ------------------------------------------------------------------
    def lock_checkout(self) -> dict:
        return self._apply(LockForCheckoutRequested(timestamp=time.time()))

    def confirm_payment(self) -> dict:
        return self._apply(PaymentConfirmed(timestamp=time.time()))

    def retry_weight(self) -> dict:
        return self._apply(RetryWeightCheckRequested(timestamp=time.time()))

    def void_pending(self) -> dict:
        return self._apply(VoidPendingItemRequested(timestamp=time.time()))

    # ------------------------------------------------------------------
    # 重置 / 結束程式
    # ------------------------------------------------------------------
    def force_logout(self, reason: str = "") -> dict:
        self._apply(ForceLogoutRequested(timestamp=time.time(), reason=reason or "工作人員手動重置"))
        self.sm.cart.clear()
        self._reset_wizard_profile()
        self._ui_stage = "main"
        return self._state_payload()

    def quit(self) -> None:
        window = self._window_holder.get("window")
        if window is not None:
            window.destroy()

    # ------------------------------------------------------------------
    # 硬體事件入口（都由背景執行緒呼叫，結果一律用 _push_state() 推給畫面）
    # ------------------------------------------------------------------
    def on_hardware_barcode(self, code: str) -> None:
        """實體 USB 條碼掃描器掃到一組條碼。

        分類規則跟 `tools/run_real_hardware_flow.py` 的 `classify_barcode()`
        一致：開頭符合 config.json 的 `state_machine.login_barcode_prefix`
        就是會員碼，否則當商品條碼。
        """
        code = (code or "").strip()
        if not code:
            return
        self._last_scan = code
        if self._login_prefix and code.startswith(self._login_prefix):
            self.login(code)
        else:
            # surface_info=True：刷條碼是使用者明確做的動作，任何被拒絕的原因
            # 都要講出來，包含狀態機只記 INFO 的那幾種（還沒進管制區、上一筆
            # 秤重比對還沒完成）。
            self._apply(ItemScanned(barcode=code, timestamp=time.time()), surface_info=True)
        self._push_state()

    def on_weight_sample(self, grams: float) -> None:
        """真實 UART/HX711 每收到一筆換算好的公克數就呼叫這裡。

        注意這裡「不會」每筆都推畫面——HX711 是 20Hz 連續送值，每筆都推會讓畫
        面每秒重畫 20 次（閃爍）。只有真的造成狀態變化或新警告（也就是比對成
        功／比對失敗）才推。
        """
        before_state = self.sm.get_session().state
        marker = self._alert_marker()
        self.sm.process_event(WeightSampleReceived(grams=grams, timestamp=time.time()))
        new_alert = self._new_alert_since(marker)
        if self.sm.get_session().state != before_state or new_alert is not None:
            if new_alert is not None and new_alert.severity != SEVERITY_INFO:
                self._last_error = new_alert.message
            self._push_state()

    def on_gate_crossing(self, crossing: str) -> None:
        """BLE 雙 Beacon 判定出一次門口穿越（見 `core/gate_monitor.py`）。"""
        self._gate_events += 1
        now = time.time()
        if crossing == GATE_CROSSING_ENTERING:
            self._apply(GateEntryDetected(timestamp=now))
        else:
            self._apply(GateExitDetected(timestamp=now))
        self._push_state()

    def on_sensor_connection_changed(self, connected: bool) -> None:
        """UART 封包斷流/恢復。"""
        now = time.time()
        if connected:
            self._apply(SensorReconnected(timestamp=now))
        else:
            self._apply(SensorDisconnected(timestamp=now))
        self._push_state()


# ----------------------------------------------------------------------
# 背景執行緒
# ----------------------------------------------------------------------
class _TimeoutTicker(threading.Thread):
    """大約每秒送一次 TimeoutTick，讓「掃碼後一直沒偵測到對應重量變化」「重量
    變了卻忘記掃碼」「付款後太久沒走出管制區」這些逾時判斷照時間自動觸發。
    """

    def __init__(self, bridge: "Bridge", stop_event: threading.Event):
        super().__init__(name="TimeoutTicker", daemon=True)
        self.bridge = bridge
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(1.0)
            try:
                before_state = self.bridge.sm.get_session().state
                marker = self.bridge._alert_marker()
                self.bridge.sm.process_event(TimeoutTick(now=time.time()))
                after = self.bridge.sm.get_session()
                new_alert = self.bridge._new_alert_since(marker)
                # 大部分 tick 什麼事都不會發生，沒必要每秒都推一次一模一樣的
                # 畫面狀態。ExitConfirm 的倒數另外用前端自己的字串顯示。
                if after.state != before_state or new_alert is not None:
                    if new_alert is not None and new_alert.severity != SEVERITY_INFO:
                        self.bridge._last_error = new_alert.message
                    self.bridge._push_state()
            except Exception:  # noqa: BLE001 — 背景執行緒，例外不能默默吃掉
                logger.exception("TimeoutTicker 內發生未預期例外")


class _BarcodeScannerConsumer(threading.Thread):
    """從實體 USB 條碼掃描器讀條碼，餵進 Bridge。"""

    def __init__(self, bridge: "Bridge", scanner, stop_event: threading.Event):
        super().__init__(name="BarcodeScannerConsumer", daemon=True)
        self.bridge = bridge
        self.scanner = scanner
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                evt = self.scanner.out_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.bridge.on_hardware_barcode(evt.code)
            except Exception:  # noqa: BLE001
                logger.exception("處理條碼時發生未預期例外（條碼：%s）", evt.code)


class _UartWeightConsumer(threading.Thread):
    """讀真的 UART 封包（BNO080 + PMW3901 + HX711 三合一），把 hx711_raw 換算成
    公克餵給狀態機，同時把 yaw_deg 餵給 GateMonitor 做門口判定的航向交叉驗證。

    封包斷流超過 `stale_timeout` 就送一次 SensorDisconnected，恢復後再送
    SensorReconnected，讓畫面上的感測器燈號跟著變。
    """

    def __init__(
        self,
        bridge: "Bridge",
        uart,
        gate_monitor: GateMonitor,
        offset: float,
        scale: float,
        stale_timeout: float,
        stop_event: threading.Event,
    ):
        super().__init__(name="UartWeightConsumer", daemon=True)
        self.bridge = bridge
        self.uart = uart
        self.gate_monitor = gate_monitor
        self.offset = offset
        self.scale = scale
        self.stale_timeout = stale_timeout
        self.stop_event = stop_event

    def run(self) -> None:
        last_packet_time = time.time()
        marked_disconnected = False
        while not self.stop_event.is_set():
            try:
                packet = self.uart.out_queue.get(timeout=0.2)
            except queue.Empty:
                if time.time() - last_packet_time > self.stale_timeout and not marked_disconnected:
                    marked_disconnected = True
                    self.bridge.on_sensor_connection_changed(False)
                continue
            last_packet_time = time.time()
            try:
                if marked_disconnected:
                    marked_disconnected = False
                    self.bridge.on_sensor_connection_changed(True)
                self.gate_monitor.update_heading(packet.yaw_deg)
                self.bridge.on_weight_sample(raw_to_grams(packet.hx711_raw, self.offset, self.scale))
            except Exception:  # noqa: BLE001
                logger.exception("處理 UART 封包時發生未預期例外")


class _GateConsumer(threading.Thread):
    """把 BLE Beacon 的 RSSI 讀數餵進 GateMonitor，判定出穿越就通知 Bridge。"""

    def __init__(self, bridge: "Bridge", scanner, gate_monitor: GateMonitor, stop_event: threading.Event):
        super().__init__(name="GateConsumer", daemon=True)
        self.bridge = bridge
        self.scanner = scanner
        self.gate_monitor = gate_monitor
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                obs = self.scanner.out_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                crossing = self.gate_monitor.process_observation(obs.beacon_id, obs.rssi, obs.timestamp)
                if crossing is not None:
                    logger.info("門口判定：%s（%s RSSI=%d）", crossing, obs.beacon_id, obs.rssi)
                    self.bridge.on_gate_crossing(crossing)
            except Exception:  # noqa: BLE001
                logger.exception("處理 BLE 觀測時發生未預期例外")


# ----------------------------------------------------------------------
def _fail(message: str) -> int:
    """硬體接不上時統一的結束方式：印出原因、結束，不要半殘地開起來。"""
    print(f"\n[錯誤] {message}\n")
    return 1


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        full_cfg = json.load(f)
    serial_cfg = full_cfg.get("serial", {})
    weight_cfg = full_cfg.get("weight", {})
    default_scanner_hint = full_cfg.get("barcode_scanner", {}).get("device_name_hint", "Barcode")
    weight_offset = weight_cfg.get("hx711_offset", 0)
    weight_scale = weight_cfg.get("hx711_scale", 1.0)
    stale_timeout = full_cfg.get("uart_protocol", {}).get("stale_data_timeout_sec", 0.5)

    parser = argparse.ArgumentParser(
        description="Phase 5：購物車觸控 UI（只走真實硬體，任一裝置接不上就不啟動）"
    )
    parser.add_argument("--db", default=None, help="資料庫路徑，預設 database/inventory.db")
    parser.add_argument("--fullscreen", action="store_true", help="全螢幕開啟（螢幕本身是 720x1280 時建議）")
    parser.add_argument("--scanner-hint", default=default_scanner_hint,
                        help=f"條碼掃描器裝置名稱關鍵字（預設讀 config.json，目前是 {default_scanner_hint!r}）")
    parser.add_argument("--port", default=serial_cfg.get("port", "/dev/ttyAMA0"), help="UART 序列埠")
    parser.add_argument("--baud", type=int, default=serial_cfg.get("baudrate", 115200), help="UART baudrate")
    parser.add_argument("--adapter", default=None, help="藍牙介面名稱，預設用系統預設（通常 hci0）")
    parser.add_argument("--beacon-timeout", type=float, default=20.0,
                        help="開機時等門口 Beacon 出現的秒數，逾時就不啟動（預設 20）")
    args = parser.parse_args()

    if not _TEMPLATE_PATH.exists():
        return _fail(f"找不到 {_TEMPLATE_PATH}。用 `python3 tools/build_app_ui.py` 產生。")

    try:
        import webview
    except ImportError:
        return _fail(
            "沒有安裝 pywebview。先 `pip install pywebview`；"
            "Pi 上如果啟動時說找不到 GTK/WebKit，再補裝："
            "`sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1`"
            "（找不到 4.1 這個套件名稱的話，改試 gir1.2-webkit2-4.0）"
        )

    if weight_scale == 1.0:
        print("[警告] config.json 的 weight.hx711_scale 還是 CALIBRATE_ME 佔位值，"
              "秤重數字不準（流程可以測，數值不能信）。用 tools/calibrate_weight.py 校正。")

    db = DBManager(args.db)
    db.init_db(seed=False)
    if not db.list_products():
        print("[提醒] 商品資料庫是空的，刷任何條碼都會顯示「查無此商品」。"
              "用 `python3 -m tools.product_admin` 建檔。")
    sm_cfg = load_state_machine_config()
    sm = CartStateMachine(db=db, cart=CartManager(), config=sm_cfg)

    landmark_cfg = load_landmark_config()
    try:
        gate_monitor = GateMonitor.from_config(landmark_cfg)
    except ValueError as exc:
        return _fail(f"門口 Beacon 設定不完整：{exc}")

    # ------------------------------------------------------------------
    # 三個硬體，任何一個接不上就不啟動
    # ------------------------------------------------------------------
    stop_event = threading.Event()
    scanner = None
    uart = None
    ble = None

    def _cleanup() -> None:
        stop_event.set()
        for dev in (scanner, uart, ble):
            if dev is not None:
                try:
                    dev.stop()
                except Exception:  # noqa: BLE001
                    pass

    # 1) 條碼掃描器
    try:
        from drivers.barcode_scanner import BarcodeScanner

        scanner = BarcodeScanner(device_name_hint=args.scanner_hint, out_queue=queue.Queue())
        scanner.start()
        print(f"[1/3] 條碼掃描器已連上（裝置名稱關鍵字：{args.scanner_hint!r}）")
    except Exception as exc:  # noqa: BLE001
        _cleanup()
        return _fail(
            f"條碼掃描器連不上：{exc}\n"
            "  1) 裝 evdev：pip install evdev\n"
            "  2) 給讀取 /dev/input/event* 的權限（做一次就好）：sudo usermod -aG input $USER，重新登入生效\n"
            "  3) 確認裝置名稱關鍵字：sudo python3 -m drivers.barcode_scanner --list"
        )

    # 2) UART（秤重 + 姿態）
    try:
        from drivers.uart_receiver import UartReceiver

        uart = UartReceiver(port=args.port, baudrate=args.baud)
        uart.start()
        deadline = time.time() + 5.0
        while time.time() < deadline and uart.out_queue.empty():
            time.sleep(0.1)
        if uart.out_queue.empty():
            raise RuntimeError(f"序列埠開起來了，但 5 秒內沒有收到任何合法封包（{args.port}）")
        print(f"[2/3] UART 已連上並收到封包（{args.port} @ {args.baud}）")
    except Exception as exc:  # noqa: BLE001
        _cleanup()
        return _fail(
            f"UART 秤重資料流連不上：{exc}\n"
            "  1) 確認下位機有在送封包，且封包是 v2 格式（8 欄位，含 squal）\n"
            f"  2) 確認 {args.port} 存在且有權限（通常要在 dialout 群組），或用 --port 指定\n"
            "  3) 單獨測一次：python3 -m drivers.uart_receiver --port " + str(args.port)
        )

    # 3) BLE 門口 Beacon
    try:
        from drivers.ble_beacon_scanner import (
            BleBeaconScanner,
            load_beacon_identity_map,
            load_beacon_ids,
        )

        ble = BleBeaconScanner(
            beacon_ids=load_beacon_ids(),
            address_map=load_beacon_identity_map(),
            out_queue=queue.Queue(),
            adapter=args.adapter,
        )
        ble.start()
        ble.wait_for_beacons(
            [gate_monitor.inside_beacon_id, gate_monitor.outside_beacon_id],
            timeout_sec=args.beacon_timeout,
        )
        print(f"[3/3] 門口 Beacon 都掃到了。{gate_monitor.describe()}")
    except Exception as exc:  # noqa: BLE001
        _cleanup()
        return _fail(
            f"BLE 門口 Beacon 連不上：{exc}\n"
            "  1) 藍牙有開嗎：sudo rfkill unblock bluetooth；systemctl status bluetooth\n"
            "  2) 裝 bleak：pip install bleak\n"
            "  3) 查實際位址/名稱：python3 -m drivers.ble_beacon_scanner --list"
        )

    # ------------------------------------------------------------------
    window_holder: dict = {"window": None}
    bridge = Bridge(db=db, sm=sm, window_holder=window_holder)

    window = webview.create_window(
        "SmartCart",
        _TEMPLATE_PATH.as_uri(),
        width=SCREEN_W,
        height=SCREEN_H,
        resizable=False,
        frameless=args.fullscreen,
        fullscreen=args.fullscreen,
        js_api=bridge,
    )
    window_holder["window"] = window

    threads = [
        _TimeoutTicker(bridge, stop_event),
        _BarcodeScannerConsumer(bridge, scanner, stop_event),
        _UartWeightConsumer(bridge, uart, gate_monitor, weight_offset, weight_scale, stale_timeout, stop_event),
        _GateConsumer(bridge, ble, gate_monitor, stop_event),
    ]

    def on_loaded():
        for t in threads:
            t.start()

    window.events.loaded += on_loaded

    # Esc 鍵跟畫面右上角 ✕ 按鈕都能正常退出；Ctrl+C 在終端機也要能乾淨結束
    # （GTK 主迴圈預設會吃掉 SIGINT）。
    signal.signal(signal.SIGINT, lambda *_: window.destroy())

    print(f"\n三個硬體都就緒。視窗 {SCREEN_W}x{SCREEN_H}，右上角 DEV 診斷面板 / ✕ 退出。\n")
    webview.start()
    _cleanup()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
