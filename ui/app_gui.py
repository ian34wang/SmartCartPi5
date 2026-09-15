"""
ui/app_gui.py

Phase 5：把已經定案的 13 頁 UI 線框稿（`ui/templates/index.html`，由
`design-cart-ui/*.dc.html` 組裝而成，功能接線跟視覺設計刻意分開處理）接上
真正的 `core.cart_state_machine.CartStateMachine` + `database.db_manager.DBManager`
+ `core.cart_manager.CartManager`，變成一支可以直接在 Pi 上執行的觸控應用。

現在整套實體硬體（管制區閘門、真的觸控螢幕安裝好、UART 線都接上）還沒辦法
組起來，所以這支程式內建一個「開發測試面板」（畫面右上角 🛠 圖示打開），把
目前還沒有硬體來源的事件（閘門進出、秤重讀數、逾時檢查）改成用面板上的按
鈕/輸入框手動觸發，讓你不用等硬體，先把「登入 -> 選擇飲食偏好精靈 -> 設定
預算 -> 購物 -> 秤重異常處理 -> 結帳 -> 出場」這一整條 UI 邏輯 + 資料庫串接
的路走通、找問題。

USB 條碼掃描器例外——那個已經是 Phase 3/4 就實機測過的硬體，所以這裡直接
接了真的（`drivers.barcode_scanner.BarcodeScanner`，開一個背景執行緒讀，見
`_BarcodeScannerConsumer`），開機時會自動嘗試連線，接不到只印警告不會擋開
機（也可以 `--no-scanner` 直接關掉這段，只用開發面板模擬）。之後閘門/秤重
硬體真的接上時，也是同樣的接法：把對應的開發面板按鈕呼叫，換成
`tools/run_real_hardware_flow.py` 那樣的真實 driver 事件來源即可——
`CartStateMachine` 本身的介面完全不用改。

（如果刷了真的條碼卻「畫面沒反應、也沒有錯誤訊息」：先檢查條碼是不是根本
不在 `database/db_manager.py` 的商品資料庫裡——資料庫目前只有出廠時寫死的
5 筆測試商品，刷任何沒建檔的真實商品條碼，狀態機會判定為「查無此商品」，
之前這支程式沒有把這種「被狀態機拒絕、但不是例外」的情況顯示出來，導致看
起來像完全沒反應，這個顯示上的洞已經補上——現在會直接秀出錯誤訊息。要把真
的商品建檔，用 `tools/product_admin.py`，見該檔案開頭說明。）

架構上刻意保持的分工：
    - `Bridge`（本檔案）：pywebview 的 js_api 物件，只做「前端呼叫 -> 轉成
      `CartStateMachine` 事件 -> 組出前端要畫的完整畫面狀態（`_state_payload`）
      再丟回去」，不含任何 UI 相關邏輯（畫面切換、選取樣式全部在
      `ui/templates/index.html` 的 JS 裡做）。
    - 前端未建模的「精靈式引導流程」（歡迎頁 -> 登入 -> 讀取個人化設定 ->
      過敏原/飲食習慣/宗教飲食/預算 四步精靈）不是 `CartStateMachine` 本身
      的狀態（那邊只有「未登入/已登入未進場/購物中/...」這種營運層級的狀
      態），所以這裡用 `self._ui_stage` 另外疊一層「畫面還沒進到正式購物
      流程之前，目前在精靈的哪一步」，等使用者完成預算設定、系統模擬「走
      進管制區」（`GateEntryDetected`）之後，才正式交給狀態機的
      `STATE_SHOPPING` 接手，`_ui_stage` 歸零。

已知但這一版刻意不假裝做到的缺口（面板上對應功能會直接跳出「尚未串接
（規劃中）」提示，不會靜悄悄地什麼都不做假裝成功）：
    1. `database/db_manager.py` 的 `members` 表目前只有 member_id/name，沒
       有欄位存過敏原/飲食習慣/預算/常購清單——所以 LoadingProfile 畫面
       「情境 A：找到已儲存設定」這個分支目前查不到真資料，選 A 一樣會走
       精靈重新問一次（並且會跳提示說明這件事），偏好精靈的結果目前只存在
       這次執行的記憶體裡，程式關掉就不見，不會寫回資料庫。
    2. Checkout 畫面「✕ 返回繼續選購」——狀態機沒有「解除鎖定」的事件（鎖
       定後只能往付款走，或整台車強制重置），所以這顆按鈕先跳提示，不會
       真的解鎖。
    3. WeightAlert 畫面「重新校準歸零」「呼叫店員協助」、ExitConfirm 畫面
       「取消本次結帳／需要協助」——都沒有對應的狀態機事件/店員通知系統，
       一樣先跳提示。

用法：
    python3 -m ui.app_gui                    # 一般視窗，720x1280，會自動嘗試接實體條碼掃描器
    python3 -m ui.app_gui --fullscreen        # 全螢幕（螢幕剛好是 720x1280 時建議）
    python3 -m ui.app_gui --db /path/to/inventory.db
    python3 -m ui.app_gui --scanner-hint USBKey   # 掃描器裝置名稱關鍵字跟預設值不一樣時指定
    python3 -m ui.app_gui --no-scanner        # 不接實體掃描器，只用開發面板模擬掃描

退出方式：畫面右上角 ✕ 按鈕（正常管道，不用重開終端機）；有接鍵盤的話 `Esc`
鍵也可以；真的卡住的話另開一個終端機 `pkill -f ui.app_gui`。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import List, Optional

# 跟 tools/preview_ui.py 同樣的理由：WebKitGTK 在部分顯示環境（尤其遠端
# VNC/螢幕分享）下硬體合成容易花屏，要在 import webview 之前設，晚了沒用。
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
    STATE_UNAUTHENTICATED,
    STATE_WEIGHT_MISMATCH_ERROR,
    TimeoutTick,
    VoidPendingItemRequested,
    WeightSampleReceived,
    load_state_machine_config,
)
from database.db_manager import DBManager, Member

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"

SCREEN_W = 720
SCREEN_H = 1280

# 精靈各步驟的合法畫面代號，wizard_goto() 只接受這些，避免前端傳錯字串時
# 悄悄把畫面切到一個 index.html 裡根本不存在的 #screen-xxx（那樣會變成整
# 頁空白，而不是一個看得出來的錯誤）。
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
    """pywebview 的 js_api 物件。每個 public 方法對應前端 `APP.callApi(name, ...)`
    呼叫的其中一種；除了 `get_state()`/`quit()` 以外，每個方法最後都回傳
    `self._state_payload()`，前端收到後直接拿來 `render()`，不需要另外再問
    一次目前狀態。
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
        prefix = cfg.get("login_barcode_prefix", "MEMBER-")
        self._guest_member_id = f"{prefix}{_GUEST_SUFFIX}" if prefix else _GUEST_SUFFIX
        self.db.upsert_member(Member(member_id=self._guest_member_id, name="訪客"))

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
        ):
            # 這三個「秤重比對中」的狀態在 UI 上都還是購物主畫面（掃碼/等待
            # 放入放回的提示框是 Shopping 畫面內的一個區塊，見
            # ui/templates/index.html 的 #f-pending-box-wrap），不是獨立畫面。
            return "shopping"
        if state == STATE_WEIGHT_MISMATCH_ERROR:
            return "weight_alert"
        if state == STATE_LOCKED_FOR_CHECKOUT:
            return "checkout"
        if state == STATE_AWAITING_EXIT:
            return "exit_confirm"
        if state == STATE_SESSION_CLOSED:
            # 走出管制區後立刻幫使用者登出、清空這次工作階段，回到歡迎頁，
            # 讓下一位使用者不用等——這一步 CartStateMachine 不會自己做
            # （它只負責記錄「這一階段結束了」，重新開始要外部呼叫
            # LogoutRequested，見檔案開頭 `_on_logout_requested` 的設計）。
            self.sm.process_event(LogoutRequested(timestamp=time.time()))
            self._reset_wizard_profile()
            self._ui_stage = "main"
            return "main"
        # STATE_LOGGED_IN_OUTSIDE_ZONE / STATE_UNAUTHENTICATED 理論上都會被
        # self._ui_stage 蓋掉（登入流程/精靈都還在 _ui_stage 範圍內），走到
        # 這裡代表不預期的狀態，保守導回歡迎頁而不是讓前端拿到不存在的畫面。
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
        # 順序很重要：_current_screen() 在偵測到 STATE_SESSION_CLOSED 時，
        # 會順手觸發登出（清空購物車、重置成一個全新的 CartSession），所以
        # 一定要先呼叫它把這個「畫面切換的副作用」做完，再去讀 session/購
        # 物車快照——不然畫面欄位已經是新的一輪、但 sm_state/購物車欄位卻
        # 還是登出前的舊資料，兩者對不起來，前端 render() 會拿到互相矛盾的
        # payload。
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
            "sm_state": s.state,
            "exit_countdown": self._exit_countdown(),
        }

    def _apply(self, event) -> dict:
        """送一個事件進狀態機，清掉上一次的錯誤訊息（每次操作都是全新的一
        次嘗試，舊錯誤不該一直黏在畫面上），回傳最新的完整畫面狀態。

        之前這裡只是單純呼叫 process_event() 就回傳畫面狀態，沒有檢查狀態機
        內部是不是其實悄悄記了一筆警告（例如條碼掃到但資料庫查無此商品、狀
        態不允許這個操作）——CartStateMachine 設計上「被拒絕的事件」不會拋
        例外，只會呼叫 self._alert() 記一筆警告然後直接 return，畫面（跟這
        支 Bridge）如果不主動去讀 get_alerts()，使用者就會看到「掃了條碼、
        畫面完全沒反應、也沒有任何錯誤訊息」——這正是實際回報的症狀（多半是
        因為刷到的是資料庫裡還沒建檔的真實商品條碼，觸發了
        ALERT_UNKNOWN_BARCODE，但這裡沒把它秀出來）。現在改成比對呼叫前後
        的警告筆數，有新警告就把最新一筆的訊息當成這次操作的結果顯示出來
        （SEVERITY_INFO 等級的通常是「忽略了一個不影響流程的事件」之類的雜
        訊，不到需要跳錯誤的程度，濾掉）。
        """
        self._last_error = None
        before = len(self.sm.get_alerts())
        self.sm.process_event(event)
        alerts = self.sm.get_alerts()
        if len(alerts) > before and alerts[-1].severity != SEVERITY_INFO:
            self._last_error = alerts[-1].message
        return self._state_payload()

    def _fail(self, message: str) -> dict:
        self._last_error = message
        return self._state_payload()

    def _push_state(self) -> None:
        """背景執行緒（真的硬體掃描器/逾時計時器）觸發的狀態變化，前端不會
        自己主動來問——一般操作是「使用者按按鈕 -> callApi() -> .then(render)」
        這種一來一回，但硬體事件是背景執行緒單方面發生的，沒有對應的前端呼
        叫可以掛 .then()，所以要反過來由 Python 主動呼叫 JS 的 APP.render()
        把最新畫面狀態推過去。真的沒有視窗/頁面還沒載入完成時安靜跳過即可，
        不是致命錯誤。
        """
        window = self._window_holder.get("window")
        if window is None:
            return
        try:
            payload = json.dumps(self._state_payload(), ensure_ascii=False)
            window.evaluate_js(f"window.APP && window.APP.render({payload})")
        except Exception:  # noqa: BLE001 — 背景執行緒，畫面推送失敗不該讓硬體執行緒整個掛掉
            logger.exception("推送畫面狀態到前端失敗")

    # ------------------------------------------------------------------
    # 畫面本身狀態查詢
    # ------------------------------------------------------------------
    def get_state(self) -> dict:
        return self._state_payload()

    def toast(self, message: str) -> dict:  # 目前只有前端自己呼叫 APP.toast()，備用
        return self._fail(message)

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
        now = time.time()
        before = self.sm.get_session().state
        self.sm.process_event(LoginScanned(member_id=member_id, timestamp=now))
        after = self.sm.get_session()
        if after.state == before and after.state != STATE_LOGGED_IN_OUTSIDE_ZONE:
            # 狀態沒變代表登入被狀態機拒絕了（格式不對／查無會員），最近一
            # 筆警告的訊息就是原因，直接秀給使用者看，不用自己重複判斷。
            alerts = self.sm.get_alerts()
            reason = alerts[-1].message if alerts else "登入失敗，請確認會員代碼"
            return self._fail(reason)
        self._ui_stage = "loading_profile"
        return self._state_payload()

    def continue_as_guest(self) -> dict:
        self._last_error = None
        now = time.time()
        self.sm.process_event(LoginScanned(member_id=self._guest_member_id, timestamp=now))
        self._ui_stage = "loading_profile"
        return self._state_payload()

    def loading_profile_choice(self, choice: str) -> dict:
        self._reset_wizard_profile()
        if choice == "A":
            self._last_error = (
                "目前資料庫還沒有欄位可以存過敏原／飲食習慣／預算等個人化設定"
                "（member 資料表只有代碼跟姓名），所以找不到「已儲存的設定」，"
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
        # 精靈完成，模擬「推車走進管制區」正式進入購物狀態——真的閘門硬體接
        # 上後，這一段要換成 tools/run_real_hardware_flow.py 那種由真實閘門
        # 感測事件觸發，而不是設完預算就自動觸發。
        now = time.time()
        self.sm.process_event(GateEntryDetected(timestamp=now))
        self._ui_stage = None
        return self._state_payload()

    # ------------------------------------------------------------------
    # 開發測試面板：模擬硬體事件
    # ------------------------------------------------------------------
    def simulate_item_scan(self, barcode: str) -> dict:
        barcode = (barcode or "").strip()
        if not barcode:
            return self._fail("請輸入條碼")
        return self._apply(ItemScanned(barcode=barcode, timestamp=time.time()))

    def simulate_weight(self, grams: str) -> dict:
        try:
            g = float(grams)
        except (TypeError, ValueError):
            return self._fail(f"重量看不懂：{grams!r}，請輸入數字")
        return self._apply(WeightSampleReceived(grams=g, timestamp=time.time()))

    def simulate_timeout(self) -> dict:
        return self._apply(TimeoutTick(now=time.time()))

    def simulate_sensor_disconnect(self) -> dict:
        return self._apply(SensorDisconnected(timestamp=time.time()))

    def simulate_sensor_reconnect(self) -> dict:
        return self._apply(SensorReconnected(timestamp=time.time()))

    def simulate_gate_exit(self) -> dict:
        return self._apply(GateExitDetected(timestamp=time.time()))

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
        self._apply(ForceLogoutRequested(timestamp=time.time(), reason=reason or "開發測試面板手動重置"))
        self.sm.cart.clear()
        self._reset_wizard_profile()
        self._ui_stage = "main"
        return self._state_payload()

    def quit(self) -> None:
        window = self._window_holder.get("window")
        if window is not None:
            window.destroy()

    # ------------------------------------------------------------------
    # 真實硬體：USB 條碼掃描器
    # ------------------------------------------------------------------
    def on_hardware_barcode(self, code: str, login_prefix: str) -> None:
        """背景執行緒（見 `_BarcodeScannerConsumer`）收到真的掃描器掃到的一
        筆條碼時呼叫。跟開發面板的差異只有「條碼分類」這一步（判斷是會員碼
        還是商品碼）——分類完之後直接借用 login()/simulate_item_scan() 既有
        邏輯，錯誤/警告的呈現方式完全一致，不用另外寫一套。這裡沒有回傳
        值，因為呼叫方是背景執行緒，不是前端的 callApi()，結果一律用
        `_push_state()` 主動推給畫面。
        """
        code = (code or "").strip()
        if not code:
            return
        if login_prefix and code.startswith(login_prefix):
            self.login(code)
        else:
            self.simulate_item_scan(code)
        self._push_state()


# ----------------------------------------------------------------------
class _TimeoutTicker(threading.Thread):
    """背景執行緒，大約每秒送一次 TimeoutTick，讓「掃碼後一直沒偵測到對應
    重量變化」「重量變了卻忘記掃碼」「結帳付款後太久沒走出管制區」這些逾時
    判斷即使沒人手動按開發面板的「手動觸發一次逾時檢查」也會照時間自動觸
    發，行為跟 tools/run_real_hardware_flow.py 的 timeout_ticker 一致。
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
                before_alerts = len(self.bridge.sm.get_alerts())
                self.bridge.sm.process_event(TimeoutTick(now=time.time()))
                after = self.bridge.sm.get_session()
                # 大部分 tick 什麼事都不會發生（還沒到逾時門檻），沒必要每秒
                # 都往前端推一次一模一樣的畫面狀態（螢幕分秒閃爍、還可能打斷
                # 使用者正在開發面板輸入框打字打到一半）——只有真的觸發了狀
                # 態變化或新警告（逾時判定成立）才推。ExitConfirm 頁面的倒數
                # 計時另外用前端自己的 payload.exit_countdown 字串顯示，不
                # 需要靠這裡每秒推播。
                if after.state != before_state or len(self.bridge.sm.get_alerts()) > before_alerts:
                    self.bridge._push_state()
            except Exception:  # noqa: BLE001 — 背景執行緒，不能讓例外悄悄吃掉整個 thread 卻沒人知道
                logger.exception("TimeoutTicker 內發生未預期例外")


class _BarcodeScannerConsumer(threading.Thread):
    """背景執行緒：從真的 USB 條碼掃描器（`drivers.barcode_scanner.BarcodeScanner`）
    讀出掃到的條碼，轉呼叫 `Bridge.on_hardware_barcode()`。這是之前這支程式
    唯一還沒接上真實硬體的地方——開發面板的「模擬掃描」輸入框一直都有接
    CartStateMachine，但實體掃描器本身完全沒有被讀取，所以刷真的商品條碼
    會「沒有任何反應」，不是邏輯錯誤，是這段線路原本就還沒接。
    """

    def __init__(self, bridge: "Bridge", scanner, login_prefix: str, stop_event: threading.Event):
        super().__init__(name="BarcodeScannerConsumer", daemon=True)
        self.bridge = bridge
        self.scanner = scanner
        self.login_prefix = login_prefix
        self.stop_event = stop_event

    def run(self) -> None:
        import queue as _queue

        while not self.stop_event.is_set():
            try:
                evt = self.scanner.out_queue.get(timeout=0.2)
            except _queue.Empty:
                continue
            try:
                self.bridge.on_hardware_barcode(evt.code, self.login_prefix)
            except Exception:  # noqa: BLE001 — 背景執行緒，不能讓例外悄悄吃掉整個 thread 卻沒人知道
                logger.exception("處理實體掃描器條碼時發生未預期例外（條碼：%s）", evt.code)


# ----------------------------------------------------------------------
def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    with open(Path(__file__).resolve().parent.parent / "config.json", "r", encoding="utf-8") as f:
        full_cfg = json.load(f)
    default_scanner_hint = full_cfg.get("barcode_scanner", {}).get("device_name_hint", "Barcode")

    parser = argparse.ArgumentParser(description="Phase 5：購物車觸控 UI 正式應用（無實體硬體時可用內建開發面板模擬）")
    parser.add_argument("--db", default=None, help="資料庫路徑，預設 database/inventory.db")
    parser.add_argument("--fullscreen", action="store_true", help="全螢幕開啟（螢幕本身就是 720x1280 時建議加這個）")
    parser.add_argument(
        "--scanner-hint", default=default_scanner_hint,
        help=f"USB 條碼掃描器的裝置名稱關鍵字（預設讀 config.json 的 barcode_scanner.device_name_hint，目前是 {default_scanner_hint!r}）",
    )
    parser.add_argument(
        "--no-scanner", action="store_true",
        help="不嘗試接實體條碼掃描器，只用畫面右上角開發面板的模擬掃描輸入框（例如在沒有接掃描器的電腦上先測 UI 邏輯時用）",
    )
    args = parser.parse_args()

    if not _TEMPLATE_PATH.exists():
        print(f"[錯誤] 找不到 {_TEMPLATE_PATH}，確認 ui/templates/index.html 有跟這支程式一起送過來")
        return 1

    try:
        import webview
    except ImportError:
        print(
            "[錯誤] 沒有安裝 pywebview。先 `pip install pywebview`；"
            "Pi 上如果啟動時說找不到 GTK/WebKit，再補裝："
            "`sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1`"
            "（找不到 4.1 這個套件名稱的話，改試 gir1.2-webkit2-4.0）"
        )
        return 1

    db = DBManager(args.db)
    db.init_db(seed=True)
    sm_cfg = load_state_machine_config()
    sm = CartStateMachine(db=db, cart=CartManager(), config=sm_cfg)

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

    stop_event = threading.Event()
    ticker = _TimeoutTicker(bridge, stop_event)

    # 實體 USB 條碼掃描器：跟 tools/run_real_hardware_flow.py 一樣「失敗不
    # 致命」——接不到掃描器（沒裝 evdev、找不到裝置、沒有讀取權限）只印警
    # 告，UI 照樣可以開、開發面板的模擬掃描照樣能用，不會因為硬體還沒接好
    # 就整支程式起不來。
    scanner = None
    scanner_thread = None
    if not args.no_scanner:
        try:
            from drivers.barcode_scanner import BarcodeScanner
            import queue as _queue

            scanner = BarcodeScanner(device_name_hint=args.scanner_hint, out_queue=_queue.Queue())
            scanner.start()
            login_prefix = sm_cfg.get("login_barcode_prefix", "")
            scanner_thread = _BarcodeScannerConsumer(bridge, scanner, login_prefix, stop_event)
            print(f"已接上條碼掃描器（裝置名稱關鍵字：{args.scanner_hint!r}）")
        except Exception as exc:  # noqa: BLE001 — 掃描器接不上不該讓整支 UI 起不來
            print(
                f"[警告] 沒有接上實體條碼掃描器（{exc}），畫面右上角開發面板的"
                "模擬掃描輸入框還是能用。真的要用實體掃描器的話，先確認："
                "1) 已安裝 evdev（pip install evdev）　"
                "2) 有讀取 /dev/input/event* 的權限（sudo usermod -aG input $USER，重新登入生效，或直接用 sudo 跑）　"
                "3) --scanner-hint 有對到掃描器的實際裝置名稱"
                "（用 `sudo python3 -m drivers.barcode_scanner --list` 查）"
            )
            scanner = None

    def on_loaded():
        ticker.start()
        if scanner_thread is not None:
            scanner_thread.start()

    window.events.loaded += on_loaded

    # Esc 鍵跟畫面右上角 ✕ 按鈕都能正常退出（不用重開終端機）；Ctrl+C 在終
    # 端機也要能乾淨結束（GTK 主迴圈預設會吃掉 SIGINT）。
    signal.signal(signal.SIGINT, lambda *_: window.destroy())

    print(f"視窗大小 {SCREEN_W}x{SCREEN_H}，畫面右上角 🛠 開發面板 / ✕ 退出。")
    webview.start()
    stop_event.set()
    if scanner is not None:
        scanner.stop()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
