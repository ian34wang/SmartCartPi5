"""
ui/app_gui.py

Phase 5：把 14 頁 UI（`ui/templates/index.html`，由 `tools/build_app_ui.py`
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

資料庫（database/db_manager.py）：
    - 會員偏好：精靈走到「開始購物」時存進 member_preferences（訪客不存）。
      LoadingProfile 選「情境 A」時讀回來，直接跳到預算那一步確認。
      「我的」分頁可以看、改預算、重新走一次精靈。
    - 交易紀錄：付款完成時寫 transactions + transaction_items，推車走出管制區
      時補上出場時間。「我的」分頁列出這位會員最近幾筆。
    - 協助請求：「呼叫店員協助」「取消本次結帳／需要協助」「重新校準歸零」
      都寫一筆 assistance_requests（沒有另外的店員通知系統，後台用
      `python3 -m tools.product_admin assist-list` 看）。店員到場刷工作證
      （config.json 的 state_machine.staff_barcode_prefix）就標成已處理。

店員工作證：條碼開頭符合 staff_barcode_prefix 時不當商品或會員碼，查 staff
資料表確認身分。秤重異常時「重新校準歸零」會把目前重量當成正確的基準——等於
接受籃子裡現在的東西，所以使用者按了只會送出申請，要店員刷工作證才會真的
歸零（狀態機的 StaffRezeroAuthorized）。

結帳「返回繼續選購」：狀態機的 UnlockCheckoutRequested。鎖定期間的重量變化
在解鎖後的第一筆讀數就會被重新核對（見該事件的說明），不是漏洞。

用法：
    python3 -m ui.app_gui                     # 四個硬體都要接好（掃描器、UART、BLE、前方相機）
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
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

# ----------------------------------------------------------------------
# WebKitGTK 繪圖設定：固定一組，不吃外面殘留的環境變數
# ----------------------------------------------------------------------
# 2026-10-29 在 Pi 5 實測（Debian 13 trixie、labwc/Wayland、DSI-1 直接接螢幕、
# Mesa 26.2.2、WebKitGTK 2.54.0），用 pywebview 開設計稿逐一切換：
#     A 合成關 + DMABUF 關  正常       B 合成關 + DMABUF 開  花屏
#     C 合成開 + DMABUF 關  正常       D 全部預設（DMABUF 開）花屏
#     E GDK_BACKEND=x11      正常       F LIBGL_ALWAYS_SOFTWARE=1 正常
# 花屏 ⇔ DMABUF 開 ∧ 硬體 GPU（V3D）∧ Wayland 原生，跟合成模式無關。
#
# 採用 C：關 DMA-BUF（網頁畫面改用共享記憶體交給 UI 行程，多一次複製，這個畫面量
# 感覺不出來），合成模式保持 WebKit 預設的開啟，網頁由 GPU 繪製。版本都是最新的也會
# 花屏，所以升級後不要拿掉。
#
# 為什麼是「強制設定 + 清掉其他的」而不是 setdefault：之前測試時在終端機設過的
# WEBKIT_*、GDK_BACKEND、LIBGL_ALWAYS_SOFTWARE 如果被 export 了，會默默留在 shell
# 裡，setdefault 會照單全收——結果同一支程式在不同終端機跑出不同的繪圖路徑，花屏
# 時根本不知道是哪一組。現在不管外面設了什麼，app 一律跑 C，被清掉的變數在啟動時
# 印出來。要再做繪圖實驗，用上面那種 pywebview 單行指令，不要改這裡。
#
# 改回 CPU 繪製（例如以後改用 VNC 看畫面又花屏）：在 _RENDER_ENV 加上
# "WEBKIT_DISABLE_COMPOSITING_MODE": "1"（＝上面的 A）。
_RENDER_ENV = {"WEBKIT_DISABLE_DMABUF_RENDERER": "1"}
# 會改變繪圖路徑、但不在 _RENDER_ENV 裡的變數一律清掉（WEBKIT_ 開頭的全部算）
_RENDER_ENV_CLEAR = ("GDK_BACKEND", "LIBGL_ALWAYS_SOFTWARE", "GALLIUM_DRIVER", "MESA_LOADER_DRIVER_OVERRIDE")


def apply_render_env(environ) -> List[str]:
    """把 environ 的繪圖相關變數設成固定的一組。回傳被清掉或改掉的「名稱=原值」。

    要在 import webview 之前呼叫（WebKit 行程啟動時才讀），本檔載入時就會套用一次。
    """
    changed = []
    for key in sorted(environ):
        if (key.startswith("WEBKIT_") or key in _RENDER_ENV_CLEAR) and key not in _RENDER_ENV:
            changed.append(f"{key}={environ[key]}")
            del environ[key]
    for key, value in _RENDER_ENV.items():
        if key in environ and environ[key] != value:
            changed.append(f"{key}={environ[key]}")
        environ[key] = value
    return changed


_RENDER_ENV_CHANGED = apply_render_env(os.environ)

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
    StaffRezeroAuthorized,
    TimeoutTick,
    UnlockCheckoutRequested,
    VoidPendingItemRequested,
    WeightSampleReceived,
    load_state_machine_config,
)
from core.gate_monitor import GATE_CROSSING_ENTERING, GateMonitor
from core.gate_recorder import GateRecorder
from core import health_passport, store_layout, store_vision
from core.landmark_correction import load_landmark_config
from core.odometry_engine import OdometryEngine
from core.weight_convert import raw_to_grams
from database.db_manager import DBManager, Member, MemberPreferences, Transaction, TransactionItem

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

SCREEN_W = 720
SCREEN_H = 1280

# 精靈各步驟的合法畫面代號。wizard_goto() 只接受這些，避免前端傳錯字串時悄悄
# 把畫面切到一個 index.html 裡根本不存在的 #screen-xxx（那會變成整頁空白，
# 而不是一個看得出來的錯誤）。
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
# 推車位置推給找貨頁地圖的間隔（秒）。整頁重畫太重，只呼叫 APP.setPos 移動那一點
_POS_PUSH_SEC = 0.5
# 機器視覺（config.json 的 vision 區塊，沒寫的用這些；意義見 core/store_vision.py）
_VISION_DEFAULTS = {
    "anchor_marker_id": 0,          # 定位點 ArUco 標記編號（DICT_4X4_50）
    "anchor_marker_size_m": 0.15,   # 標記黑框實際邊長（m），印出來量
    "anchor_trigger_m": 1.5,        # 標記離推車（水平）這麼近以內才採用，越近越準
    "camera_height_m": 0.5,         # 相機離地高度；由標記算出來的高度差太多（height_tol_m）就當誤判
    "height_tol_m": 0.15,
    "vp_yaw_weight": 0.2,           # 消失點修航向的權重 w
    "vp_min_lines": 4,              # 消失點至少要幾條線支持
    "vp_max_angle_deg": 28.0,       # 消失點在光軸左右幾度內才採用（約半個水平視角）
    "fps": 5.0,                     # 每秒處理幾張
    "proc_width": 640,              # 縮到這個寬度再處理
}
# 推車不在管制區裡時，地圖上不顯示推車位置（對齊作廢）
_OUT_OF_ZONE_STATES = (STATE_UNAUTHENTICATED, STATE_LOGGED_IN_OUTSIDE_ZONE, STATE_SESSION_CLOSED)
_RECENT_TX_LIMIT = 5


def _fmt_time(t: Optional[float]) -> str:
    return "" if t is None else time.strftime("%m/%d %H:%M", time.localtime(t))


def summary_or_empty(found) -> str:
    return health_passport.summary(found) if found else ""


def _fmt_budget(b: Optional[float]) -> str:
    return "不限" if b is None else f"${b:.0f}"

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

    def __init__(self, db: DBManager, sm: CartStateMachine, window_holder: dict, gate_side=None,
                 store: Optional[dict] = None, vision: Optional[dict] = None):
        self.db = db
        self.sm = sm
        self._window_holder = window_holder
        # 回傳門口判定目前確認的一側："a" 門內 / "b" 門外 / None 還沒確認。登入當下推車已經在
        # 門內的話，直接算進場（見 _enter_if_inside）。測試時不給就是不知道。
        self._gate_side = gate_side
        # 店面平面圖（config.json 的 store 區塊，見 core/store_layout.py）與推車位置對齊。
        # _pose 是里程計最新一筆 (X mm, Y mm, yaw deg, t)，UART 執行緒寫、其他執行緒讀，用鎖保護。
        self._layout = store_layout.load(store)
        self._frame = store_layout.StoreFrame(self._layout)
        self._pose: Optional[tuple] = None
        self._pose_lock = threading.Lock()
        self._pos_sent = None
        # 消失點修航向的互補濾波權重 w（config.json 的 vision.vp_yaw_weight）
        self._vp_weight = float((vision or {}).get("vp_yaw_weight", _VISION_DEFAULTS["vp_yaw_weight"]))
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

    def _current_screen(self) -> str:
        s = self.sm.get_session()
        state = s.state
        # 秤重異常永遠優先：就算使用者正在（重新）走偏好精靈，也要先處理異常，
        # 不能讓精靈畫面蓋住防損畫面。處理完之後精靈從原本那一步繼續。
        if state == STATE_WEIGHT_MISMATCH_ERROR:
            return "weight_alert"
        if self._ui_stage is not None:
            return self._ui_stage
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
            if self._open_tx_id is not None:
                try:
                    self.db.mark_transaction_exited(self._open_tx_id, s.last_activity_at or time.time())
                except sqlite3.Error:
                    logger.exception("交易 %s 的出場時間寫入失敗", self._open_tx_id)
                self._open_tx_id = None
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
                logger.warning("商品 %s 的貨架位置 %r 不在平面圖上（代碼見 core/store_layout.py），找貨頁不顯示",
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
        with self._pose_lock:
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
        """背景執行緒（硬體）觸發的狀態變化，主動推給前端重畫。

        一般操作是「使用者按按鈕 -> callApi() -> .then(render)」一來一回，但
        硬體事件是背景執行緒單方面發生的，沒有對應的前端呼叫可以掛 .then()。
        視窗還沒建立/頁面還沒載入完成時安靜跳過即可，不是致命錯誤。
        """
        window = self._window_holder.get("window")
        if window is None:
            return
        try:
            # ensure_ascii=True（中文變成 \uXXXX）是必要的，不是風格選擇：pywebview 的 GTK 版
            # evaluate_js 把「字元數」當成 WebKit 要的「位元組數」傳進去，腳本裡只要有中文
            # （UTF-8 一個字 3 bytes）就會被截斷，WebKit 報 "SyntaxError: Unexpected end of
            # script"，畫面停在舊狀態——2026-11-16 實測：會員名稱「測試會員」進了 payload
            # 之後，BLE 判定進場、狀態機也轉成購物中了，畫面卻一直顯示等待進入管制區。
            # 全部 ASCII 時字元數 = 位元組數；\uXXXX 在 JS 裡解析出來的字串完全一樣。
            payload = json.dumps(self._state_payload(), ensure_ascii=True)
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
        self._enter_if_inside()
        self._ui_stage = "loading_profile"
        return self._state_payload()

    def continue_as_guest(self) -> dict:
        self._last_error = None
        self.sm.process_event(LoginScanned(member_id=self._guest_member_id, timestamp=time.time()))
        self._enter_if_inside()
        self._ui_stage = "loading_profile"
        return self._state_payload()

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
        # 雙 Beacon 判定的（見 _GateConsumer），這裡只是把畫面交給購物主畫面，
        # 狀態機仍然停在「已登入、未進管制區」，畫面會顯示等待提示。
        self._ui_stage = None
        return self._state_payload()

    # ------------------------------------------------------------------
    # 「我的」分頁
    # ------------------------------------------------------------------
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
    def lock_checkout(self) -> dict:
        return self._apply(LockForCheckoutRequested(timestamp=time.time()))

    def unlock_checkout(self) -> dict:
        return self._apply(UnlockCheckoutRequested(timestamp=time.time()))

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

    def request_assistance(self, kind: str = "general") -> dict:
        if kind not in _ASSIST_REASONS:
            return self._fail(f"不認得的協助類型：{kind}")
        self._last_error = None
        if self._create_request(_ASSIST_REASONS[kind]) and self._notice is None:
            self._notice = "已通知店員，請在原地稍候（店員到場刷工作證即完成處理）"
        return self._state_payload()

    def request_rezero(self) -> dict:
        if self.sm.get_session().state != STATE_WEIGHT_MISMATCH_ERROR:
            return self._fail("目前不是秤重異常，不需要重新校準歸零")
        self._last_error = None
        if self._create_request(_REZERO_REASON):
            self._staff_pending = "rezero"
            if self._notice is None:
                self._notice = "已送出歸零申請，請店員刷工作證確認"
        return self._state_payload()

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

    def retry_weight(self) -> dict:
        return self._apply(RetryWeightCheckRequested(timestamp=time.time()))

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
    def force_logout(self, reason: str = "") -> dict:
        self._apply(ForceLogoutRequested(timestamp=time.time(), reason=reason or "工作人員手動重置"))
        self.sm.cart.clear()
        self._reset_wizard_profile()
        self._open_tx_id = None
        self._ui_stage = "main"
        with self._pose_lock:
            self._frame.clear()
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

        分類規則：開頭符合 config.json 的 `state_machine.staff_barcode_prefix`
        是店員工作證；符合 `login_barcode_prefix` 是會員碼（跟
        `tools/run_real_hardware_flow.py` 的 `classify_barcode()` 一致）；
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
            self._check_budget()
            self._push_state()

    def on_gate_crossing(self, crossing: str) -> None:
        """BLE 雙 Beacon 判定出一次門口穿越（見 `core/gate_monitor.py`）。"""
        self._gate_events += 1
        now = time.time()
        if crossing == GATE_CROSSING_ENTERING:
            self._apply(GateEntryDetected(timestamp=now))
            # 推車剛進門、在長廊裡：先暫時對齊地圖，走到定位點停住再正式對齊（core/store_layout.py）
            with self._pose_lock:
                if self._pose is not None and self.sm.get_session().state not in _OUT_OF_ZONE_STATES:
                    self._frame.enter(*self._pose[:3])
        else:
            self._apply(GateExitDetected(timestamp=now))
            with self._pose_lock:
                self._frame.clear()
        self._push_state()

    def on_pose(self, x_mm: float, y_mm: float, yaw_deg: float, t: float) -> None:
        """里程計每算出一筆位置就呼叫（UART 執行緒）。"""
        out_of_zone = self.sm.get_session().state in _OUT_OF_ZONE_STATES
        with self._pose_lock:
            self._pose = (x_mm, y_mm, yaw_deg, t)
            if out_of_zone:
                if self._frame.aligned:
                    self._frame.clear()
                return
            self._frame.feed(x_mm, y_mm)

    def _anchor_locked(self, at=None) -> bool:
        """用最新的里程計位置對齊（呼叫端持有 _pose_lock）。回傳這次是不是從沒對齊變成對齊。"""
        was = self._frame.status
        self._frame.anchor(*self._pose[:3], at=at)
        self._frame.feed(*self._pose[:2])
        return was != store_layout.StoreFrame.ANCHORED

    def on_anchor_seen(self, sighting) -> None:
        """相機看到地上的定位點標記（_VisionConsumer，sighting 是 core/store_vision.AnchorSighting）：
        推車位置 = 定位點 − (右方, 前方)（航向固定朝店內，右方 = +x、前方 = +y），正式對齊。"""
        if self.sm.get_session().state in _OUT_OF_ZONE_STATES:
            return
        a, u = self._layout["anchor"], store_layout.unit_m(self._layout)
        at = (float(a["x"]) - sighting.lateral_m / u, float(a["y"]) - sighting.forward_m / u)
        with self._pose_lock:
            if self._pose is None:
                return
            first = self._anchor_locked(at)
        if first:
            self._notice = f"已在{a['name']}對齊地圖，推車位置會顯示在找貨頁"
            logger.info("看到定位點標記（前方 %.2f m、右方 %.2f m、相機高 %.2f m），地圖對齊完成",
                        sighting.forward_m, sighting.lateral_m, sighting.height_m)
            self._push_state()

    def on_vp_heading(self, delta_deg: float) -> None:
        """貨架消失點的方向 δ（_VisionConsumer）→ 修正平面圖航向（core/store_layout.StoreFrame.correct_heading）。"""
        with self._pose_lock:
            if self._pose is None or self._frame.status != store_layout.StoreFrame.ANCHORED:
                return
            x, y, yaw, _ = self._pose
            theta_v = store_vision.heading_from_vp(delta_deg, self._frame.heading(yaw))
            e = self._frame.correct_heading(x, y, yaw, theta_v, self._vp_weight)
        if e is not None:
            logger.debug("消失點修正航向：誤差 %.1f°", e)

    def anchor_here(self) -> dict:
        """找貨頁「我在定位點」：相機沒對到標記時（例如標記被擋住），客人推到定位點手動對齊。"""
        if self.sm.get_session().state in _OUT_OF_ZONE_STATES:
            return self._fail("推車還沒進入管制區，進門後再對齊")
        with self._pose_lock:
            if self._pose is None:
                return self._fail("還沒收到里程計資料（UART），沒辦法對齊")
            self._anchor_locked()
        self._last_error = None
        self._notice = f"已在{self._layout['anchor']['name']}對齊地圖"
        return self._state_payload()

    def push_pos(self) -> None:
        """把推車位置推給找貨頁地圖（_PosPusher 定時呼叫，位置有變才推）。"""
        pos = self._cart_pos_payload()
        if pos == self._pos_sent:
            return
        window = self._window_holder.get("window")
        if window is None:
            return
        self._pos_sent = pos
        try:
            window.evaluate_js(f"window.APP && window.APP.setPos && window.APP.setPos({json.dumps(pos, ensure_ascii=True)})")
        except Exception:  # noqa: BLE001
            logger.exception("推送推車位置到前端失敗")

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
        odometry: Optional[OdometryEngine] = None,
    ):
        super().__init__(name="UartWeightConsumer", daemon=True)
        self.odometry = odometry
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
                if self.odometry is not None:
                    est = self.odometry.process_packet(packet)
                    self.bridge.on_pose(est.x_mm, est.y_mm, est.yaw_deg, time.time())
            except Exception:  # noqa: BLE001
                logger.exception("處理 UART 封包時發生未預期例外")


class _VisionConsumer(threading.Thread):
    """前方相機：每張影像找定位點標記與貨架消失點（core/store_vision.py），結果交給 Bridge。"""

    def __init__(self, bridge: "Bridge", camera, detector, vcfg: dict, stop_event: threading.Event):
        super().__init__(name="VisionConsumer", daemon=True)
        self.bridge = bridge
        self.camera = camera
        self.detector = detector
        self.cfg = vcfg
        self.stop_event = stop_event

    def run(self) -> None:
        period = 1.0 / max(0.5, float(self.cfg["fps"]))
        failures = 0
        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                gray = self.camera.read_gray()
                failures = 0
                sight = self.detector.detect(gray)
                if (sight is not None and sight.ground_m <= float(self.cfg["anchor_trigger_m"])
                        and abs(sight.height_m - float(self.cfg["camera_height_m"])) <= float(self.cfg["height_tol_m"])):
                    self.bridge.on_anchor_seen(sight)
                h, w = gray.shape[:2]
                vp = store_vision.vanishing_point(store_vision.detect_lines(gray), w, h,
                                                  min_lines=int(self.cfg["vp_min_lines"]))
                if vp is not None:
                    delta = store_vision.vp_offset_deg(vp.u, vp.v, self.camera.K)
                    if abs(delta) <= float(self.cfg["vp_max_angle_deg"]):
                        self.bridge.on_vp_heading(delta)
            except Exception:  # noqa: BLE001
                failures += 1
                if failures in (1, 10, 100):
                    logger.exception("相機影像處理失敗（連續第 %d 次）", failures)
            self.stop_event.wait(max(0.0, period - (time.time() - t0)))


class _PosPusher(threading.Thread):
    """每 _POS_PUSH_SEC 秒把推車位置推給找貨頁地圖。"""

    def __init__(self, bridge: "Bridge", stop_event: threading.Event):
        super().__init__(name="PosPusher", daemon=True)
        self.bridge = bridge
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.wait(_POS_PUSH_SEC):
            self.bridge.push_pos()


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
        store_layout.unit_m(store_layout.load(full_cfg.get("store")))
    except ValueError as exc:
        return _fail(f"店面平面圖設定不完整：{exc}")

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
        # 每一筆 Beacon 讀數與判定結果都記錄到 data/gate_logs/（core/gate_recorder.py），
        # 事後用 python3 -m tools.analyze_gate_log 分析
        gate_monitor = GateMonitor.from_config(landmark_cfg, recorder=GateRecorder.for_runtime("app"))
    except ValueError as exc:
        return _fail(f"門口 Beacon 設定不完整：{exc}")

    # ------------------------------------------------------------------
    # 四個硬體，任何一個接不上就不啟動
    # ------------------------------------------------------------------
    stop_event = threading.Event()
    scanner = None
    uart = None
    ble = None
    camera = None

    def _cleanup() -> None:
        stop_event.set()
        for dev in (scanner, uart, ble, camera):
            if dev is not None:
                try:
                    dev.stop()
                except Exception:  # noqa: BLE001
                    pass
        gate_monitor.close()

    # 1) 條碼掃描器
    try:
        from drivers.barcode_scanner import BarcodeScanner

        scanner = BarcodeScanner(device_name_hint=args.scanner_hint, out_queue=queue.Queue())
        scanner.start()
        print(f"[1/4] 條碼掃描器已連上（裝置名稱關鍵字：{args.scanner_hint!r}）")
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
        print(f"[2/4] UART 已連上並收到封包（{args.port} @ {args.baud}）")
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
        print(f"[3/4] 門口 Beacon 都掃到了。{gate_monitor.describe()}")
    except Exception as exc:  # noqa: BLE001
        _cleanup()
        return _fail(
            f"BLE 門口 Beacon 連不上：{exc}\n"
            "  1) 藍牙有開嗎：sudo rfkill unblock bluetooth；systemctl status bluetooth\n"
            "  2) 裝 bleak：pip install bleak\n"
            "  3) 查實際位址/名稱：python3 -m drivers.ble_beacon_scanner --list"
        )

    # 4) 前方相機（定位點標記、貨架消失點 → 找貨頁的推車位置）
    vision_cfg = dict(_VISION_DEFAULTS, **{k: v for k, v in full_cfg.get("vision", {}).items() if not k.startswith("_")})
    try:
        from core.store_vision import AnchorDetector
        from drivers.front_camera import FrontCamera

        camera = FrontCamera(full_cfg.get("camera", {}), proc_width=int(vision_cfg["proc_width"]))
        anchor_detector = AnchorDetector(vision_cfg["anchor_marker_id"], vision_cfg["anchor_marker_size_m"], camera.K)
        print(f"[4/4] 前方相機已開（處理 {camera.size[0]}x{camera.size[1]}，"
              f"定位點標記 #{vision_cfg['anchor_marker_id']}、邊長 {vision_cfg['anchor_marker_size_m']} m）")
    except Exception as exc:  # noqa: BLE001
        _cleanup()
        return _fail(
            f"前方相機開不起來：{exc}\n"
            "  1) picamera2 與 OpenCV：sudo apt install python3-picamera2 python3-opencv\n"
            "  2) 相機排線、rpicam-hello --list-cameras 看得到相機嗎\n"
            "  3) 校正檔：python3 -m drivers.camera_stream --calibrate（config.json 的 camera.calibration_file）"
        )

    # ------------------------------------------------------------------
    window_holder: dict = {"window": None}
    bridge = Bridge(db=db, sm=sm, window_holder=window_holder,
                    gate_side=lambda: gate_monitor.snapshot()["dominant"], store=full_cfg.get("store"),
                    vision=vision_cfg)
    # 里程計（光流 + IMU，參數是 tools/calibrate_optical_flow.py 在長廊校正寫回的）→ 找貨頁的推車位置
    odo_cfg = full_cfg.get("odometry", {})
    if odo_cfg.get("optical_flow_px_to_mm", 1.0) == 1.0:
        print("[警告] config.json 的 odometry.optical_flow_px_to_mm 還是 CALIBRATE_ME 佔位值，"
              "找貨頁的推車位置不準。用 tools/calibrate_optical_flow.py 在長廊校正。")
    odometry = OdometryEngine.from_config(odo_cfg)

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
        _UartWeightConsumer(bridge, uart, gate_monitor, weight_offset, weight_scale, stale_timeout, stop_event,
                            odometry=odometry),
        _PosPusher(bridge, stop_event),
        _VisionConsumer(bridge, camera, anchor_detector, vision_cfg, stop_event),
        _GateConsumer(bridge, ble, gate_monitor, stop_event),
    ]

    def on_loaded():
        for t in threads:
            t.start()

    window.events.loaded += on_loaded

    # Esc 鍵跟畫面右上角 ✕ 按鈕都能正常退出；Ctrl+C 在終端機也要能乾淨結束
    # （GTK 主迴圈預設會吃掉 SIGINT）。
    signal.signal(signal.SIGINT, lambda *_: window.destroy())

    if _RENDER_ENV_CHANGED:
        print("[提醒] 忽略終端機殘留的繪圖設定（app 固定用 GPU 繪製、關 DMA-BUF）："
              + "、".join(_RENDER_ENV_CHANGED))
    print(f"\n四個硬體都就緒。視窗 {SCREEN_W}x{SCREEN_H}，右上角 DEV 診斷面板 / ✕ 退出。\n")
    webview.start()
    _cleanup()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
