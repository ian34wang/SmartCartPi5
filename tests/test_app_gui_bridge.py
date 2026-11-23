"""ui.app_gui.Bridge 的端對端測試——不需要 pywebview/GTK，也不需要任何硬體。

這支測的是「硬體事件進來之後，狀態機與畫面狀態的反應對不對」。硬體本身的
存在與否不在測試範圍內（那是 app_gui.py 啟動時的硬性檢查），所以這裡直接呼叫
Bridge 上那三個硬體入口方法：

    bridge.on_hardware_barcode(code)   <- 正式運作時由 evdev 掃描器執行緒呼叫
    bridge.on_weight_sample(grams)     <- 正式運作時由 UART 執行緒呼叫
    bridge.on_gate_crossing(crossing)  <- 正式運作時由 BLE 執行緒呼叫

——用的是跟正式運作完全一樣的入口，不是另外開一條測試專用的旁路。

跑法（在專案根目錄）：
    python3 tests/test_app_gui_bridge.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.cart_manager import CartManager
from core.cart_state_machine import (
    CartStateMachine, load_state_machine_config,
    STATE_SHOPPING, STATE_LOGGED_IN_OUTSIDE_ZONE, STATE_WEIGHT_MISMATCH_ERROR,
    STATE_LOCKED_FOR_CHECKOUT, STATE_AWAITING_EXIT, STATE_UNAUTHENTICATED,
)
from database.db_manager import DBManager
from ui.app_gui import Bridge

FAILURES = []


def check(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {extra}")
        FAILURES.append(label)


def logic_config():
    """config.json 的狀態機參數，但關掉秤重穩定窗（settle_window_sec = 0）。

    這支測的是流程邏輯，每次只餵一筆讀數；正式的 config.json 在 11-16 校正後
    設了 settle_window_sec > 0（讀數要穩定 W_s 秒才拿來比對），單筆讀數永遠
    湊不出穩定值。穩定窗本身由 tests/test_calibrate_weight.py 用真實錄音重播驗證。
    """
    cfg = load_state_machine_config()
    cfg["settle_window_sec"] = 0.0
    return cfg


# 測試用平面圖：一格 0.5 m（正式值是 config.json 的 store.unit_m，現場量）
TEST_STORE = {"unit_m": 0.5}


def new_bridge():
    db = DBManager(tempfile.mktemp(suffix=".db"))
    db.init_db(seed=True)   # 測試需要那 5 筆測試商品
    sm = CartStateMachine(db=db, cart=CartManager(), config=logic_config())
    return Bridge(db=db, sm=sm, window_holder={"window": None}, store=TEST_STORE)


def to_shopping(bridge, budget="1000"):
    """走完精靈，再讓 BLE 判定進場，進入真正的購物狀態。"""
    bridge.continue_as_guest()
    bridge.loading_profile_choice("B")
    for stage in ("diet", "religion", "budget"):
        bridge.wizard_goto(stage)
    bridge.set_budget(budget)
    bridge.on_gate_crossing("entering")
    bridge.on_weight_sample(0.0)   # 秤持續送值，建立基準
    return bridge.get_state()


# ======================================================================
print("\n[1] 精靈走完之後，還沒通過閘門前不算開始購物")
b = new_bridge()
b.continue_as_guest()
b.loading_profile_choice("B")
for stage in ("diet", "religion", "budget"):
    b.wizard_goto(stage)
p = b.set_budget("1000")
check(p["screen"] == "shopping", "畫面切到購物主畫面", p["screen"])
check(p["sm_state"] == STATE_LOGGED_IN_OUTSIDE_ZONE, "狀態機仍是「已登入未進管制區」", p["sm_state"])
check(p["awaiting_gate_entry"] is True, "畫面會顯示等待進入管制區")
b.on_weight_sample(0.0)
p = b.get_state()
b.on_hardware_barcode("4710018001234")
p = b.get_state()
check(bool(p["last_error"]), "進場前掃碼會被拒絕且有錯誤訊息", repr(p["last_error"]))
check(p["cart_count"] == 0, "沒有偷偷加進購物車")

print("\n[2] BLE 判定進場後才真的開始購物")
b.on_gate_crossing("entering")
p = b.get_state()
check(p["sm_state"] == STATE_SHOPPING, "狀態轉成購物中", p["sm_state"])
check(p["awaiting_gate_entry"] is False, "等待提示消失")
check(p["gate_events"] == 1, "門口判定次數有累計", p["gate_events"])

print("\n[3] 掃碼 + 秤重比對成功（先掃碼再放入＝加入）")
b.on_hardware_barcode("4710018001234")   # 統一陽光豆漿 300ml, 320g ±15g
p = b.get_state()
check(not p["last_error"], "掃碼被接受", repr(p["last_error"]))
check(p["pending_item_barcode"] == "4710018001234", "進入等待秤重比對")
check(p["pending_item_mode"] == "add", "判定為加入", p["pending_item_mode"])
b.on_weight_sample(320.0)
p = b.get_state()
check(p["cart_count"] == 1, "商品進購物車", p["cart_count"])
check(abs(p["cart_total"] - 25.0) < 0.01, "金額正確", p["cart_total"])
check(p["sm_state"] == STATE_SHOPPING, "回到購物中", p["sm_state"])

print("\n[4] 沒有秤重讀數時掃碼會被拒絕，而且錯誤要看得見")
b2 = new_bridge()
b2.continue_as_guest()
b2.loading_profile_choice("B")
for stage in ("diet", "religion", "budget"):
    b2.wizard_goto(stage)
b2.set_budget("unlimited")
b2.on_gate_crossing("entering")     # 進場了，但 UART 一筆讀數都還沒送來
p = b2.get_state()
check(p["has_weight_reading"] is False, "目前確實沒有秤重讀數")
b2.on_hardware_barcode("4710018001234")
p = b2.get_state()
check(bool(p["last_error"]), "掃碼被拒絕時有錯誤訊息（不能靜悄悄沒反應）", repr(p["last_error"]))
check("秤重" in (p["last_error"] or ""), "錯誤訊息講的是秤重沒資料", repr(p["last_error"]))

print("\n[5] 未建檔的條碼：要明確說查無商品")
b3 = new_bridge()
to_shopping(b3)
b3.on_hardware_barcode("9999999999999")
p = b3.get_state()
check(bool(p["last_error"]), "有錯誤訊息", repr(p["last_error"]))
check("查無" in (p["last_error"] or ""), "訊息內容是查無商品", repr(p["last_error"]))
check(p["last_scan"] == "9999999999999", "條碼有被記錄下來（診斷用）", p["last_scan"])

print("\n[6] 條碼分類：會員碼要走登入，不是當商品掃")
b4 = new_bridge()
b4.go_to_login()
b4.on_hardware_barcode("MEMBER-0001")
p = b4.get_state()
check(p["screen"] == "loading_profile", "會員條碼觸發登入流程", p["screen"])
check(p["member_name"] == "測試會員 A", "登入到正確的會員", p["member_name"])
b4.on_hardware_barcode("MEMBER-NOPE")
p = b4.get_state()
check(bool(p["last_error"]), "不存在的會員碼要報錯", repr(p["last_error"]))

print("\n[7] 先變重量再掃碼＝移除")
b5 = new_bridge()
to_shopping(b5)
b5.on_hardware_barcode("4710428061234")   # 多力多滋 74g
b5.on_weight_sample(74.0)
p = b5.get_state()
check(p["cart_count"] == 1, "先加入一件", p["cart_count"])
b5.on_weight_sample(0.0)                  # 拿出來，重量掉回去
b5.on_hardware_barcode("4710428061234")
p = b5.get_state()
check(not p["last_error"], "拿出後掃碼沒有被拒絕", repr(p["last_error"]))
b5.on_weight_sample(0.0)
p = b5.get_state()
check(p["cart_count"] == 0, "商品已從購物車移除", p["cart_count"])

print("\n[8] 秤重不符進入異常畫面，放棄後回到購物")
b6 = new_bridge()
to_shopping(b6)
b6.on_hardware_barcode("4710428061234")   # 74g ±5g
b6.on_weight_sample(400.0)                # 差太多
p = b6.get_state()
check(p["sm_state"] == STATE_WEIGHT_MISMATCH_ERROR, "進入秤重異常", p["sm_state"])
check(p["screen"] == "weight_alert", "切到秤重異常畫面", p["screen"])
check(bool(p["last_error"]), "異常有訊息", repr(p["last_error"]))
p = b6.void_pending()
check(p["sm_state"] == STATE_SHOPPING, "放棄這筆後回到購物中", p["sm_state"])

print("\n[9] 鎖定結帳 -> 付款 -> BLE 判定出場 -> 自動登出")
b7 = new_bridge()
to_shopping(b7)
b7.on_hardware_barcode("4711080012345")   # 泰山礦泉水 605g
b7.on_weight_sample(605.0)
p = b7.get_state()
check(p["cart_count"] == 1, "商品已加入", p["cart_count"])
p = b7.lock_checkout()
check(p["sm_state"] == STATE_LOCKED_FOR_CHECKOUT and p["screen"] == "checkout", "鎖定結帳", p["sm_state"])
p = b7.confirm_payment()
check(p["sm_state"] == STATE_AWAITING_EXIT and p["screen"] == "exit_confirm", "付款完成", p["sm_state"])
check(p["exit_countdown"] is not None, "有出場倒數")
b7.on_gate_crossing("exiting")
p = b7.get_state()
check(p["sm_state"] == STATE_UNAUTHENTICATED and p["screen"] == "main", "出場後自動登出回歡迎頁", p["sm_state"])
check(p["cart_count"] == 0, "購物車已清空")

print("\n[10] 感測器斷線 / 恢復")
b8 = new_bridge()
to_shopping(b8)
b8.on_sensor_connection_changed(False)
p = b8.get_state()
check(p["sensor_connected"] is False, "畫面知道感測器斷了")
b8.on_hardware_barcode("4710018001234")
p = b8.get_state()
check(bool(p["last_error"]), "斷線時掃碼要報錯", repr(p["last_error"]))
b8.on_sensor_connection_changed(True)
p = b8.get_state()
check(p["sensor_connected"] is True, "恢復後燈號回復")

print("\n[11] 精靈流程與偏好選取")
b9 = new_bridge()
b9.go_to_login()
b9.on_hardware_barcode("MEMBER-0002")
p = b9.loading_profile_choice("A")
check(p["screen"] == "allergens", "沒有已儲存的設定時，選 A 走精靈", p["screen"])
check("還沒有儲存過設定" in (p["last_error"] or ""), "有說明這個會員還沒有已儲存的設定", repr(p["last_error"]))
p = b9.wizard_toggle_allergen("花生")
check("花生" in p["wizard_profile"]["allergens"], "過敏原可勾選")
p = b9.wizard_toggle_allergen("花生")
check("花生" not in p["wizard_profile"]["allergens"], "過敏原可取消勾選")
b9.wizard_goto("diet")
p = b9.wizard_set_diet("蛋奶素")
check(p["wizard_profile"]["diet"] == "蛋奶素", "飲食習慣可設定")
b9.wizard_goto("religion")
p = b9.wizard_toggle_religion("伊斯蘭教（清真／Halal）")
check("伊斯蘭教（清真／Halal）" in p["wizard_profile"]["religion"], "宗教飲食可勾選")
b9.wizard_goto("budget")
p = b9.set_budget("500")
check(p["budget"] == 500.0, "預算設定完成", p["budget"])
p = b9.wizard_goto("不存在的步驟")
check(bool(p["last_error"]), "不合法的精靈步驟要擋下來")

print("\n[12] 警告歷史塞滿之後，錯誤訊息仍然要浮得上來（回歸測試）")
# CartStateMachine 的警告清單有上限（預設 200 筆），滿了之後每新增一筆就砍掉
# 最舊的一筆，總長度不變。Bridge 以前是用「筆數有沒有變多」判斷有沒有新警告，
# 所以跑滿 200 筆之後就再也偵測不到新錯誤，畫面又會退回「掃了沒反應也沒錯誤」。
b10 = new_bridge()
to_shopping(b10)
for _ in range(210):
    b10.on_hardware_barcode("9999999999999")
check(len(b10.sm.get_alerts()) == 200, "警告清單確實已達上限", len(b10.sm.get_alerts()))
b10.on_hardware_barcode("9999999999999")
p = b10.get_state()
check(bool(p["last_error"]), "清單塞滿後新的錯誤仍然顯示得出來", repr(p["last_error"]))

print("\n[13] 會員偏好：精靈走完存起來，下次選 A 直接沿用")
db13 = b9.db
prefs = db13.get_preferences("MEMBER-0002")
check(prefs is not None and prefs.diet == "蛋奶素" and prefs.budget == 500.0
      and prefs.religion == ["伊斯蘭教（清真／Halal）"], "精靈結果已寫進資料庫", prefs)
b9.force_logout("測試換人")
b9.go_to_login()
b9.on_hardware_barcode("MEMBER-0002")
p = b9.get_state()
check(p["saved_profile"]["exists"] is True and p["saved_profile"]["diet"] == "蛋奶素",
      "登入後載入畫面看得到已儲存的設定", p["saved_profile"])
p = b9.loading_profile_choice("A")
check(p["screen"] == "budget", "選 A 直接跳到預算確認", p["screen"])
check(p["wizard_profile"]["diet"] == "蛋奶素" and p["budget"] == 500.0, "讀回已儲存的偏好與預算",
      (p["wizard_profile"], p["budget"]))
check(not p["last_error"], "沒有錯誤訊息", repr(p["last_error"]))
bg = new_bridge()
bg.continue_as_guest()
p = bg.loading_profile_choice("A")
check(p["screen"] == "allergens" and "訪客" in (p["last_error"] or ""), "訪客選 A：說明訪客沒有已儲存的設定")
for stage in ("diet", "religion", "budget"):
    bg.wizard_goto(stage)
bg.set_budget("1000")
check(bg.db.get_preferences(bg._guest_member_id) is None, "訪客的設定不會存進資料庫")

print("\n[14] 結帳返回：解除鎖定，鎖定期間的重量變化在解鎖後被抓到")
b14 = new_bridge()
to_shopping(b14)
p = b14.lock_checkout()
check(p["screen"] == "checkout", "鎖定結帳", p["screen"])
b14.on_weight_sample(300.0)                # 鎖定期間偷放東西
p = b14.unlock_checkout()
check(p["sm_state"] == STATE_SHOPPING and p["screen"] == "shopping", "解鎖回到購物中", p["sm_state"])
b14.on_weight_sample(300.0)
p = b14.get_state()
check(p["sm_state"] == "awaiting_item_scan", "解鎖後立刻偵測到未掃碼的重量變化", p["sm_state"])
p = b14.unlock_checkout()
check(p["sm_state"] == "awaiting_item_scan", "沒有鎖定時解鎖不會改變狀態", p["sm_state"])

print("\n[15] 交易紀錄：付款寫一筆，出場補出場時間，「我的」看得到")
b15 = new_bridge()
b15.go_to_login()
b15.on_hardware_barcode("MEMBER-0001")
b15.loading_profile_choice("B")
for stage in ("diet", "religion", "budget"):
    b15.wizard_goto(stage)
b15.set_budget("1000")
b15.on_gate_crossing("entering")
b15.on_weight_sample(0.0)
b15.on_hardware_barcode("4710018001234")   # 25 元，320g
b15.on_weight_sample(320.0)
b15.on_hardware_barcode("4711080012345")   # 15 元，605g
b15.on_weight_sample(925.0)
b15.lock_checkout()
p = b15.confirm_payment("LINE Pay")
check(p["sm_state"] == STATE_AWAITING_EXIT and not p["last_error"], "付款完成", (p["sm_state"], p["last_error"]))
txs = b15.db.list_transactions(member_id="MEMBER-0001")
check(len(txs) == 1 and abs(txs[0].total - 40.0) < 1e-9 and txs[0].item_count == 2
      and txs[0].payment_method == "LINE Pay" and txs[0].exited_at is None, "寫進一筆交易（金額、件數、付款方式）",
      txs)
check([i.barcode for i in txs[0].items] == ["4710018001234", "4711080012345"], "交易明細正確", txs[0].items)
check(txs[0].zone_entered_at is not None and txs[0].budget == 1000.0, "記下進場時間與預算")
check(len(p["recent_transactions"]) == 1, "「我的」最近消費紀錄看得到", p["recent_transactions"])
b15.on_gate_crossing("exiting")
p = b15.get_state()
check(p["screen"] == "main", "出場後回歡迎頁")
check(b15.db.list_transactions(member_id="MEMBER-0001")[0].exited_at is not None, "出場時間已補上")
b15b = new_bridge()
to_shopping(b15b)
p = b15b.confirm_payment("LINE Pay")      # 沒有鎖定就付款：狀態機拒絕
check(b15b.db.list_transactions() == [], "狀態機拒絕付款時不會寫交易")

print("\n[16] 呼叫店員協助：寫一筆待處理，店員刷工作證處理掉")
b16 = new_bridge()
to_shopping(b16)
p = b16.request_assistance("general")
check(p["assist_open"] is not None and p["notice"], "畫面顯示已通知店員", (p["assist_open"], p["notice"]))
p = b16.request_assistance("general")
check(len(b16.db.list_assistance_requests(status="open")) == 1, "同一個原因不會重複寫")
p = b16.request_assistance("亂寫")
check(bool(p["last_error"]), "不認得的協助類型要擋下來")
b16.on_hardware_barcode("STAFF-9999")
p = b16.get_state()
check("不在店員名單" in (p["last_error"] or "") and p["assist_open"] is not None, "不認得的工作證不能處理請求",
      repr(p["last_error"]))
check(p["cart_count"] == 0 and p["sm_state"] == STATE_SHOPPING, "工作證不會被當成商品條碼")
b16.on_hardware_barcode("STAFF-0001")
p = b16.get_state()
check(p["assist_open"] is None and "測試店員" in (p["notice"] or ""), "店員刷工作證後請求標成已處理",
      (p["assist_open"], p["notice"]))
r = b16.db.list_assistance_requests()[0]
check(r.status == "resolved" and r.resolved_by == "STAFF-0001" and r.sm_state == STATE_SHOPPING,
      "資料庫記下誰處理、當時狀態", r)

print("\n[17] 重新校準歸零：要店員刷工作證才生效")
b17 = new_bridge()
to_shopping(b17)
b17.on_hardware_barcode("4710428061234")   # 74g ±5g
b17.on_weight_sample(74.0)
check(b17.get_state()["cart_count"] == 1, "先放一件")
b17.on_hardware_barcode("4710018001234")   # 320g
b17.on_weight_sample(500.0)                # 不符
p = b17.get_state()
check(p["screen"] == "weight_alert", "進入秤重異常")
p = b17.request_rezero()
check(p["staff_pending"] == "rezero" and p["sm_state"] == STATE_WEIGHT_MISMATCH_ERROR,
      "使用者按下只送出申請，狀態不變", (p["staff_pending"], p["sm_state"]))
check(p["assist_open"] is not None, "同時寫一筆協助請求")
b17.on_hardware_barcode("STAFF-9999")
p = b17.get_state()
check(p["sm_state"] == STATE_WEIGHT_MISMATCH_ERROR, "不認得的工作證不能歸零")
b17.on_hardware_barcode("STAFF-0001")
p = b17.get_state()
check(p["sm_state"] == STATE_SHOPPING and p["screen"] == "shopping", "店員刷工作證後回到購物中", p["sm_state"])
check(p["cart_count"] == 1, "待比對的那件沒有被加入", p["cart_count"])
check(abs(b17.sm.get_session().stable_weight_g - 500.0) < 1e-9, "目前重量成為新的基準")
check(any(a.code == "staff_rezero_authorized" and "STAFF-0001" in a.message for a in b17.sm.get_alerts()),
      "警告歷史記下是哪位店員確認的")
check(p["assist_open"] is None and p["staff_pending"] is None, "協助請求一併處理掉")
b17.on_weight_sample(500.0)
check(b17.get_state()["sm_state"] == STATE_SHOPPING, "之後同樣重量不會再觸發異常")
b17b = new_bridge()
to_shopping(b17b)
p = b17b.request_rezero()
check(bool(p["last_error"]) and p["staff_pending"] is None, "不是秤重異常時不能申請歸零")
b17c = new_bridge()
to_shopping(b17c)
b17c.on_hardware_barcode("4710428061234")
b17c.on_weight_sample(400.0)
b17c.request_rezero()
p = b17c.void_pending()                    # 使用者自己放棄了
check(p["staff_pending"] is None, "異常自己解除後，歸零申請跟著取消")
b17c.on_hardware_barcode("STAFF-0001")
check(not any(a.code == "staff_rezero_authorized" for a in b17c.sm.get_alerts()), "之後刷工作證不會歸零")

print("\n[18] 「我的」：改預算、重新設定偏好、秤重異常優先")
b18 = new_bridge()
b18.go_to_login()
b18.on_hardware_barcode("MEMBER-0001")
b18.loading_profile_choice("B")
b18.wizard_toggle_allergen("花生")
for stage in ("diet", "religion", "budget"):
    b18.wizard_goto(stage)
b18.set_budget("1000")
b18.on_gate_crossing("entering")
b18.on_weight_sample(0.0)
p = b18.set_session_budget("1500")
check(p["budget"] == 1500.0 and b18.db.get_preferences("MEMBER-0001").budget == 1500.0, "改預算，也存成下次的預設")
p = b18.set_session_budget("abc")
check(bool(p["last_error"]) and p["budget"] == 1500.0, "看不懂的金額要擋下來")
p = b18.edit_preferences()
check(p["screen"] == "allergens", "重新設定偏好走精靈", p["screen"])
b18.wizard_toggle_allergen("芝麻")
p = b18.wizard_goto("login")               # 第一步按上一步＝取消
check(p["screen"] == "shopping" and p["wizard_profile"]["allergens"] == ["花生"], "取消會還原原本的設定",
      (p["screen"], p["wizard_profile"]))
b18.edit_preferences()
b18.wizard_toggle_allergen("芝麻")
b18.on_hardware_barcode("4710428061234")
b18.on_weight_sample(400.0)                # 精靈進行中發生秤重異常
p = b18.get_state()
check(p["screen"] == "weight_alert", "秤重異常蓋過精靈畫面", p["screen"])
p = b18.void_pending()
check(p["screen"] == "allergens", "異常處理完回到精靈原本那一步", p["screen"])
for stage in ("diet", "religion", "budget"):
    b18.wizard_goto(stage)
p = b18.set_budget("1500")
check(p["screen"] == "shopping" and p["notice"] == "偏好設定已更新", "完成後回購物畫面", (p["screen"], p["notice"]))
check(b18.db.get_preferences("MEMBER-0001").allergens == ["花生", "芝麻"], "新的偏好已存檔")
b18.lock_checkout()
p = b18.edit_preferences()
check(bool(p["last_error"]) and p["screen"] == "checkout", "結帳鎖定中不能改偏好", p["screen"])

print("\n[19] 繪圖設定固定：終端機殘留的測試變數不會影響 app")
import ui.app_gui as app_mod  # noqa: E402
dirty = {"WEBKIT_DISABLE_COMPOSITING_MODE": "1", "WEBKIT_DISABLE_DMABUF_RENDERER": "0",
         "GDK_BACKEND": "x11", "LIBGL_ALWAYS_SOFTWARE": "1", "WEBKIT_SKIA_ENABLE_CPU_RENDERING": "1",
         "HOME": "/home/x", "WAYLAND_DISPLAY": "wayland-0"}
changed = app_mod.apply_render_env(dirty)
check(dirty == {"WEBKIT_DISABLE_DMABUF_RENDERER": "1", "HOME": "/home/x", "WAYLAND_DISPLAY": "wayland-0"},
      "只留下固定的那一組，其他繪圖變數全部清掉，無關的變數不動", dirty)
check(len(changed) == 5, "每一個被清掉或改掉的都回報出來", changed)
clean = {"WEBKIT_DISABLE_DMABUF_RENDERER": "1"}
check(app_mod.apply_render_env(clean) == [] and clean == {"WEBKIT_DISABLE_DMABUF_RENDERER": "1"},
      "已經是正確設定時什麼都不報")
import os  # noqa: E402
check(os.environ.get("WEBKIT_DISABLE_DMABUF_RENDERER") == "1" and "WEBKIT_DISABLE_COMPOSITING_MODE" not in os.environ,
      "載入 ui.app_gui 時已經套用（GPU 繪製、關 DMA-BUF）")

print("\n[20] 硬體事件主動推給畫面的腳本必須全是 ASCII（pywebview GTK 會用字元數當位元組數截斷）")


class _FakeWindow:
    def __init__(self):
        self.scripts = []

    def evaluate_js(self, script):
        self.scripts.append(script)


import json as _json  # noqa: E402
fw = _FakeWindow()
b20 = new_bridge()
b20._window_holder["window"] = fw
b20.go_to_login()
b20.on_hardware_barcode("MEMBER-0001")          # 會員名稱「測試會員 A」是中文
b20.on_gate_crossing("entering")
check(fw.scripts and all(sc.isascii() for sc in fw.scripts), "推送的腳本沒有任何非 ASCII 字元",
      [sc for sc in fw.scripts if not sc.isascii()][:1])
last = fw.scripts[-1]
pl = _json.loads(last[len("window.APP && window.APP.render("):-1])
check(pl["member_name"] == "測試會員 A", "\\uXXXX 解回來的中文跟原本一樣", pl["member_name"])

print("\n[21] 健康護照：規則、掃碼提醒、購物清單標記")
from core import health_passport as hp  # noqa: E402
check([c.reason for c in hp.conflicts(["花生"], allergens=["花生"])] == ["過敏原：花生"], "過敏原直接比對")
check(len(hp.conflicts(["蛋", "牛奶／羊奶"], diet="蛋奶素")) == 0, "蛋奶素可以吃蛋、奶")
check({c.tag for c in hp.conflicts(["蛋", "牛奶／羊奶", "五辛"], diet="全素（純素）")} == {"蛋", "牛奶／羊奶", "五辛"},
      "全素：蛋、奶、五辛都衝突")
check({c.tag for c in hp.conflicts(["魚類"], diet="蛋奶素")} == {"魚類"}, "魚類算海鮮，素食會提醒")
check({c.tag for c in hp.conflicts(["豬肉", "酒精", "牛肉"], religion=["伊斯蘭教（清真／Halal）"])} == {"豬肉", "酒精"},
      "清真：豬肉、酒精")
check(not hp.conflicts(["花生"], allergens=["都沒有"]), "「都沒有」不會衝突")
from database.db_manager import Product  # noqa: E402
b21 = new_bridge()
b21.db.upsert_product(Product("PEANUT", "花生醬", 80.0, 340.0, 20.0, tags=["花生", "大豆"], shelf="M3"))
b21.continue_as_guest()
b21.loading_profile_choice("B")
b21.wizard_toggle_allergen("花生")
for stage in ("diet", "religion", "budget"):
    b21.wizard_goto(stage)
b21.set_budget("1000")
b21.on_gate_crossing("entering")
b21.on_weight_sample(0.0)
b21.on_hardware_barcode("PEANUT")
p = b21.get_state()
check(p["pending_item"]["health"] == "含花生（過敏原：花生）", "比對中的商品標出衝突", p["pending_item"])
b21.on_hardware_barcode("PEANUT")       # 已經在比對中：不重複提醒（被狀態機忽略）
b21.on_weight_sample(340.0)
p = b21.get_state()
check(p["cart_count"] == 1 and "花生" in p["cart_items"][0]["health"], "只提醒不阻擋：照樣加入，清單上標出來",
      p["cart_items"])

print("\n[22] 特價：結帳金額套用折扣")
b22 = new_bridge()
b22.db.upsert_product(Product("SALE", "即期沙拉", 60.0, 250.0, 25.0, discount_pct=50, shelf="L3"))
to_shopping(b22)
b22.on_hardware_barcode("SALE")
p = b22.get_state()
check(p["pending_item"]["price"] == 30.0 and p["pending_item"]["list_price"] == 60.0, "比對中顯示特價與原價",
      p["pending_item"])
b22.on_weight_sample(250.0)
p = b22.get_state()
check(p["cart_total"] == 30.0 and p["cart_list_total"] == 60.0, "總額用特價，原價合計另外給", (p["cart_total"], p["cart_list_total"]))
b22.lock_checkout()
b22.confirm_payment("LINE Pay")
tx = b22.db.list_transactions()[0]
check(tx.total == 30.0 and tx.items[0].unit_price == 30.0, "交易紀錄記的是實收金額", tx)

print("\n[23] 錢包管家：超過 80%、100% 各提醒一次")
b23 = new_bridge()
to_shopping(b23, budget="40")
b23.on_hardware_barcode("4710018001234")   # 25 元
b23.on_weight_sample(320.0)
p = b23.get_state()
check(p["notice"] is None and p["budget_remaining"] == 15.0, "62%：不提醒", (p["notice"], p["budget_remaining"]))
b23.on_hardware_barcode("4711080012345")   # 15 元 → 40 = 100%
b23.on_weight_sample(925.0)
p = b23.get_state()
check(p["notice"] and "100%" in p["notice"], "到 80% 以上提醒一次", p["notice"])
b23.on_hardware_barcode("4710036122345")   # 30 元 → 70 > 40
b23.on_weight_sample(970.0)
p = b23.get_state()
check(p["notice"] and "超過本次預算" in p["notice"] and p["budget_remaining"] == -30.0, "超過預算再提醒", p["notice"])
p = b23.set_session_budget("200")
p = b23.set_session_budget("50")
check("超過本次預算" in (p["notice"] or ""), "改預算後重新判斷", p["notice"])

print("\n[24] 優惠頁：特價、搭配、依偏好與預算推薦，全部從資料庫算")
b24 = new_bridge()
db24 = b24.db
db24.upsert_product(Product("MILK", "鮮奶", 90.0, 950.0, 50.0, shelf="M4", pairs_with=["CEREAL"]))
db24.upsert_product(Product("CEREAL", "穀片", 120.0, 300.0, 30.0, shelf="R3"))
db24.upsert_product(Product("SALAD", "即期沙拉", 60.0, 250.0, 25.0, discount_pct=50, shelf="L3"))
db24.upsert_product(Product("NUTS", "綜合堅果", 150.0, 200.0, 20.0, tags=["堅果類"], shelf="R1"))
db24.upsert_product(Product("WINE", "紅酒", 900.0, 1200.0, 60.0, tags=["酒精"], shelf="L1"))
b24.continue_as_guest()
b24.loading_profile_choice("B")
b24.wizard_toggle_allergen("堅果類")
for stage in ("diet", "religion", "budget"):
    b24.wizard_goto(stage)
b24.set_budget("500")
b24.on_gate_crossing("entering")
b24.on_weight_sample(0.0)
b24.on_hardware_barcode("MILK")
b24.on_weight_sample(950.0)
promos = b24.get_state()["promotions"]
kinds = {c["barcode"]: c["kind"] for c in promos}
check(kinds.get("SALAD") == "discount", "有折扣的商品出現在特價", kinds)
check(kinds.get("CEREAL") == "pair", "購物車裡鮮奶的搭配（穀片）出現", kinds)
check("NUTS" not in kinds, "跟過敏原衝突的不推薦", kinds)
check("WINE" not in kinds, "超過剩餘預算（410）的不推薦", kinds)
check("MILK" not in kinds, "已經在購物車的不推薦", kinds)
check(all(c["can_nav"] for c in promos if c["kind"] != "discount"), "推薦的都可以導航去貨架")
cat = {c["barcode"]: c for c in b24.get_state()["catalog"]}
check(cat["MILK"]["in_cart"] and cat["NUTS"]["health"] and cat["SALAD"]["shelf"] == "L3", "找貨頁的商品資料齊全",
      cat["NUTS"])

print("\n[25] 沒掃碼的重量變化要放棄：要店員刷工作證")
from core.cart_state_machine import TimeoutTick  # noqa: E402
import time as _time  # noqa: E402
b25 = new_bridge()
to_shopping(b25)
b25.on_weight_sample(300.0)                         # 放了東西沒掃碼
b25.sm.process_event(TimeoutTick(now=_time.time() + 30))
p = b25.get_state()
check(p["sm_state"] == STATE_WEIGHT_MISMATCH_ERROR and p["pending_item_barcode"] is None, "逾時進入秤重異常")
p = b25.void_pending()
check(p["sm_state"] == STATE_WEIGHT_MISMATCH_ERROR and p["staff_pending"] == "void" and p["assist_open"],
      "使用者按放棄只送出申請，狀態不變", (p["sm_state"], p["staff_pending"]))
b25.on_hardware_barcode("STAFF-0001")
p = b25.get_state()
check(p["sm_state"] == STATE_SHOPPING and p["assist_open"] is None and "放棄" in (p["notice"] or ""),
      "店員刷工作證後才放棄", (p["sm_state"], p["notice"]))
b25b = new_bridge()
to_shopping(b25b)
b25b.on_hardware_barcode("4710428061234")
b25b.on_weight_sample(400.0)                        # 掃過碼、重量不符
p = b25b.void_pending()
check(p["sm_state"] == STATE_SHOPPING and p["staff_pending"] is None, "掃過碼的那一筆不用店員就能放棄", p["sm_state"])

print("\n[26] 登入時推車已經在門內：直接算進場")
side = {"v": "a"}
db26 = DBManager(tempfile.mktemp(suffix=".db"))
db26.init_db(seed=True)
b26 = Bridge(db=db26, sm=CartStateMachine(db=db26, cart=CartManager(), config=logic_config()),
             window_holder={"window": None}, gate_side=lambda: side["v"])
p = b26.continue_as_guest()
check(p["sm_state"] == STATE_SHOPPING, "BLE 已確認在門內 → 登入就進場", p["sm_state"])
b26.force_logout("換人")
side["v"] = "b"
p = b26.continue_as_guest()
check(p["sm_state"] == STATE_LOGGED_IN_OUTSIDE_ZONE, "在門外 → 照常等穿越門口", p["sm_state"])

print("\n[27] 舊資料庫自動補欄位；product_admin 的標籤／貨架／折扣")
import sqlite3 as _sq  # noqa: E402
old = tempfile.mktemp(suffix=".db")
c = _sq.connect(old)
c.execute("CREATE TABLE products (barcode TEXT PRIMARY KEY, name TEXT NOT NULL, unit_price REAL NOT NULL, "
          "standard_weight_g REAL NOT NULL, weight_tolerance_g REAL NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
c.execute("INSERT INTO products VALUES ('/3R8T2CX','手機',1.0,195.0,19.5,'x')")
c.commit()
c.close()
dbo = DBManager(old)
dbo.init_db(seed=False)
po = dbo.get_product("/3R8T2CX")
check(po is not None and po.tags == [] and po.shelf == "" and po.discount_pct == 0, "舊資料庫補上欄位、原資料還在", po)
import tools.product_admin as pa  # noqa: E402
meta = pa.parse_meta({"tags": "花生;大豆", "shelf": "m3", "discount_pct": "30"}, "/3R8T2CX", po)
check(meta == {"tags": ["花生", "大豆"], "shelf": "M3", "discount_pct": 30.0, "pairs_with": []}, "解析標籤、貨架、折扣", meta)
for bad in ({"tags": "鹽"}, {"shelf": "B2"}, {"shelf": "M5"}, {"discount_pct": "120"}):
    try:
        pa.parse_meta(bad, "X", None)
        check(False, f"錯誤的值要擋下來：{bad}")
    except ValueError:
        check(True, f"錯誤的值要擋下來：{bad}")
dbo.upsert_product(pa.with_meta(po, meta))
kept = pa.parse_meta({}, "/3R8T2CX", dbo.get_product("/3R8T2CX"))
check(kept["tags"] == ["花生", "大豆"], "沒給的欄位沿用原本的（重秤商品不會清掉標籤）", kept)
csvp = tempfile.mktemp(suffix=".csv")
pa.export_csv(dbo, csvp)
db2 = DBManager(tempfile.mktemp(suffix=".db"))
db2.init_db(seed=False)
ok, errs = pa.import_csv(db2, csvp)
p2 = db2.get_product("/3R8T2CX")
check(ok == 1 and not errs and p2.tags == ["花生", "大豆"] and p2.shelf == "M3" and p2.discount_pct == 30,
      "CSV 匯出再匯入，標籤等都在", (ok, errs, p2))

print("\n[28] 店面平面圖：貨架代碼、路線、推車位置在定位點對齊")
import math as _m  # noqa: E402
from core import store_layout as sl  # noqa: E402
lay = sl.load(TEST_STORE)
check(sl.codes(lay)[:4] == ["L1", "L2", "L3", "L4"] and len(sl.codes(lay)) == 12, "三座貨架各 4 個角", sl.codes(lay))
check(sl.describe(lay, "m3") == "中架・左下", "代碼 → 中文位置", sl.describe(lay, "m3"))
r = sl.route(lay, "L2")
check(r[0] == (0.0, 0.0) and r[1] == (0, 4.25) and r[-1] == (-2, 7.5), "路線：門 → 定位點 → … → 貨架後側", r)
check(all(lay["hall"][0] <= x <= lay["hall"][1] and y <= lay["hall"][3] for x, y in r), "路線不出店外", r)
check("往右走到右架" in sl.instruction(lay, "R4") and "前側" in sl.instruction(lay, "R4"), "說明文字",
      sl.instruction(lay, "R4"))
custom = sl.load({"slots": {"F": "前面", "B": "後面上"}, "_comment": "x"})
check(sl.codes(custom)[:2] == ["LF", "LB"] and "_comment" not in custom, "config 可以改分法", sl.codes(custom))


for bad in (None, 0, -1, "abc"):
    try:
        sl.unit_m(sl.load({"unit_m": bad}))
        check(False, f"unit_m = {bad!r} 要報錯")
    except ValueError:
        pass
try:
    fr0 = sl.StoreFrame(sl.load())
    fr0.anchor(0.0, 0.0, 90.0)
    fr0.feed(100.0, 0.0)
    check(False, "沒填 unit_m 時換算推車位置要報錯（不能用沒量過的預設值）")
except ValueError as exc:
    check("store.unit_m" in str(exc), "沒填 unit_m：報錯並指出 config.json 的 store.unit_m", str(exc))
check(sl.unit_m(sl.load({"unit_m": 0.6})) == 0.6, "unit_m 照 config 的值")

fr = sl.StoreFrame(lay)
fr.enter(1000.0, 1000.0, 30.0)                      # 進場：里程計航向 30° = 店內方向
check(fr.status == fr.ROUGH, "進場：暫時對齊")
fx_, fy_ = fr.feed(1000.0 + 500 * _m.cos(_m.radians(40)), 1000.0 + 500 * _m.sin(_m.radians(40)))
check(fx_ == 0.0 and 1.9 < fy_ < 2.0, "長廊鎖住方向：還沒到定位點前 x 固定在中線", (fx_, fy_))
fr.anchor(3000.0, 2000.0, 30.0)
check(fr.status == fr.ANCHORED and fr.feed(3000.0, 2000.0) == (0.0, 4.25), "看到定位點：對齊到定位點", fr.last)
check(abs(fr.heading(30.0) - 90.0) < 1e-9, "航向固定成正向（朝店內 = 90°）", fr.heading(30.0))
px, py = fr.to_store(3000.0 + 1000 * _m.cos(_m.radians(30)), 2000.0 + 1000 * _m.sin(_m.radians(30)))
check(abs(px) < 1e-6 and abs(py - 6.25) < 1e-6, "沿航向直走 1 m = 往店內 2 格", (px, py))
px, py = fr.to_store(3000.0 + 1000 * _m.cos(_m.radians(120)), 2000.0 + 1000 * _m.sin(_m.radians(120)))
check(abs(px + 2) < 1e-6 and abs(py - 4.25) < 1e-6, "左轉 90° 走 1 m → 往左 2 格", (px, py))
# IMU 漂了 5°：車子其實還是朝店內（90°），IMU 卻說 35°（估計 95°）。消失點量到 90°。
before = fr.to_store(3000.0, 2000.0)
e = fr.correct_heading(3000.0, 2000.0, 35.0, 90.0, weight=1.0)
check(abs(e - (-5.0)) < 1e-9 and abs(fr.heading(35.0) - 90.0) < 1e-9, "消失點修航向：誤差 −5°，修完 = 90°", (e, fr.heading(35.0)))
check(fr.to_store(3000.0, 2000.0) == before, "修航向時推車目前位置不跳")
check(fr.correct_heading(3000.0, 2000.0, 35.0, 120.0, weight=1.0) is None, "誤差超過 15° 不採用（多半是線找錯）")
check(sl.StoreFrame(lay).correct_heading(0, 0, 0, 90, 1.0) is None, "還沒對齊不修航向")

print("\n[29] 機器視覺：地上的定位點標記、貨架消失點（相機 50 cm 高、往下俯）")
import numpy as _np  # noqa: E402
import cv2 as _cv2  # noqa: E402
from core import store_vision as sv  # noqa: E402
W_, H_ = 640, 360
K_ = _np.array([[450.0, 0, 320], [0, 450.0, 180], [0, 0, 1]])


def render_scene(beta_deg, cam_h=0.5, gamma_deg=None, marker=None, marker_id=0, size_m=0.15):
    """測試用合成影像。世界座標 X 右、Y 前（車頭）、Z 上；相機在 (0, 0, cam_h) 往下俯 beta。
    gamma：畫一組跟車頭夾 gamma 度（往右為正）的水平線（地磚縫、貨架層板），加幾條垂直邊。
    marker：(前方, 右方, 旋轉角) 把 ArUco 標記貼在地上。"""
    b = _m.radians(beta_deg)
    R = _np.stack([[1, 0, 0], [0, -_m.sin(b), -_m.cos(b)], [0, _m.cos(b), -_m.sin(b)]])
    C = _np.array([0, 0, cam_h])

    def proj(P):
        pc = R @ (_np.asarray(P, dtype=float) - C)
        return (K_[0, 0] * pc[0] / pc[2] + K_[0, 2], K_[1, 1] * pc[1] / pc[2] + K_[1, 2]), pc[2]

    out = _np.full((H_, W_), 150, _np.uint8)
    if gamma_deg is not None:
        g = _m.radians(gamma_deg)
        d, nrm = _np.array([_m.sin(g), _m.cos(g), 0]), _np.array([_m.cos(g), -_m.sin(g), 0])
        for off in (-1.4, -0.9, 0.9, 1.4):
            for z in (0.0, 0.25, 0.8):
                (p1, z1), (p2, z2) = proj(off * nrm + 1.2 * d + [0, 0, z]), proj(off * nrm + 6 * d + [0, 0, z])
                if z1 > 0 and z2 > 0:
                    _cv2.line(out, tuple(map(int, p1)), tuple(map(int, p2)), 30, 2)
            for k in (1.5, 2.5):
                (p1, _), (p2, _) = proj(off * nrm + k * d), proj(off * nrm + k * d + [0, 0, 0.8])
                _cv2.line(out, tuple(map(int, p1)), tuple(map(int, p2)), 30, 2)
    if marker is not None:
        fwd, right, rot = marker
        mk = sv.make_marker(marker_id, 300)
        hf = size_m * mk.shape[0] / 300 / 2                 # 含白邊的半邊長
        c_, s_ = _m.cos(_m.radians(rot)), _m.sin(_m.radians(rot))
        dst = [proj([right + c_ * ox - s_ * oy, fwd + s_ * ox + c_ * oy, 0])[0]
               for ox, oy in ((-hf, hf), (hf, hf), (hf, -hf), (-hf, -hf))]
        src = _np.float32([[0, 0], [mk.shape[1], 0], [mk.shape[1], mk.shape[0]], [0, mk.shape[0]]])
        Hm = _cv2.getPerspectiveTransform(src, _np.float32(dst))
        warped = _cv2.warpPerspective(mk, Hm, (W_, H_))
        mask = _cv2.warpPerspective(_np.full_like(mk, 255), Hm, (W_, H_))
        out[mask > 0] = warped[mask > 0]
    return out


det = sv.AnchorDetector(0, 0.15, K_)
for beta, fwd, right, rot in ((30, 1.0, 0.1, 0), (35, 0.8, -0.2, 20), (25, 1.4, 0.0, -10)):
    sg = det.detect(render_scene(beta, marker=(fwd, right, rot)))
    check(sg is not None and abs(sg.forward_m - fwd) < 0.05 and abs(sg.lateral_m - right) < 0.03
          and abs(sg.height_m - 0.5) < 0.03,
          f"地上標記（俯角 {beta}°、轉 {rot}°）：前方 {fwd} m、右方 {right} m、相機高 0.5 m", sg)
check(sv.AnchorDetector(1, 0.15, K_).detect(render_scene(30, marker=(1.0, 0.1, 0))) is None, "編號不對的標記不算")
check(abs(sv.vp_offset_deg(320 + 450 * _m.tan(_m.radians(8)) / _m.cos(_m.radians(30)),
                           180 - 450 * _m.tan(_m.radians(30)), K_) - 8.0) < 1e-9,
      "δ 公式：俯角 30°、水平夾角 8° 的消失點 → δ = 8°")
for beta, gamma in ((30, 6.0), (25, -10.0), (35, 0.0)):
    sc = render_scene(beta, gamma_deg=gamma)
    vp = sv.vanishing_point(sv.detect_lines(sc), W_, H_)
    dlt = None if vp is None else sv.vp_offset_deg(vp.u, vp.v, K_)
    check(dlt is not None and abs(dlt - gamma) < 1.0, f"合成貨架（俯角 {beta}°）：消失點量到 δ ≈ {gamma}°", dlt)
check(sv.vanishing_point(sv.detect_lines(_np.full((H_, W_), 150, _np.uint8)), W_, H_) is None, "空白影像沒有消失點")
check(abs(sv.heading_from_vp(5.0, 97.0) - 95.0) < 1e-9, "θ = φ + δ，φ 取最近的座標軸（90°）")
check(abs(sv.heading_from_vp(-3.0, 182.0) - 177.0) < 1e-9, "朝 −x 方向時 φ = 180°")

print("\n[29b] 現場驗證工具（tools/verify_store_vision.py）的量測與彙整")
from tools import verify_store_vision as vsv  # noqa: E402
from ui.app_gui import _VISION_DEFAULTS  # noqa: E402
vc_ = dict(_VISION_DEFAULTS)
sc = render_scene(30, gamma_deg=4.0, marker=(1.0, 0.1, 0))
r_ = vsv.measure(sc, det, K_, vc_)
check(r_["anchor"] is not None and r_["anchor"]["accepted"] and abs(r_["anchor"]["forward_m"] - 1.0) < 0.05,
      "標記：前方 1 m、app 會採用", r_["anchor"])
check(r_["vp"] is not None and abs(r_["vp"]["delta_deg"] - 4.0) < 1.0 and abs(r_["vp"]["beta_deg"] - 30.0) < 1.5,
      "消失點：δ ≈ 4°、俯角 β ≈ 30°", r_["vp"])
r_far = vsv.measure(render_scene(25, marker=(1.4, 0.0, 0), cam_h=0.5), det, K_, dict(vc_, anchor_trigger_m=1.2))
check(r_far["anchor"] is not None and not r_far["anchor"]["accepted"], "超過 anchor_trigger_m：看到但不採用", r_far["anchor"])
r_h = vsv.measure(sc, det, K_, dict(vc_, camera_height_m=0.8))
check(not r_h["anchor"]["accepted"], "算出的相機高跟 config 差太多：不採用", r_h["anchor"])
blank = vsv.measure(_np.full((H_, W_), 150, _np.uint8), det, K_, vc_)
s_ = vsv.summarize([r_, r_, blank])
check(s_["frames"] == 3 and s_["anchor_frames"] == 2 and s_["vp_frames"] == 2
      and abs(s_["forward_m"] - r_["anchor"]["forward_m"]) < 1e-12, "彙整：偵測張數與中位數", s_)
check(vsv.summarize([blank])["forward_m"] is None, "都沒看到：中位數是 None（不補值）")

print("\n[30] 推車位置接進 app：進場、定位點、消失點、出場作廢")
b28 = new_bridge()
b28.db.upsert_product(Product("TEA", "綠茶", 20.0, 600.0, 30.0, shelf="R2"))
b28.db.upsert_product(Product("OLD", "舊代碼", 20.0, 600.0, 30.0, shelf="B2"))   # 舊九宮格代碼直接寫進 DB
cat = {c["barcode"]: c for c in b28.get_state()["catalog"]}
check(cat["TEA"]["shelf_text"] == "右架・右上" and cat["TEA"]["route"][-1] == [3, 7.5] and cat["TEA"]["instruction"],
      "找貨頁拿到中文位置、路線、說明", cat["TEA"])
check(cat["OLD"]["shelf"] == "" and cat["OLD"]["route"] == [], "不在平面圖上的代碼不顯示（不亂畫）", cat["OLD"])
st = b28.get_state()
check(set(st["store"]) == {"hall", "corridor", "anchor", "shelves", "slots"}, "payload 帶平面圖", list(st["store"]))
b28.on_pose(0.0, 0.0, 90.0, 0.0)
check(b28.get_state()["cart_pos"] == {"status": "none"}, "還沒登入：沒有推車位置")
p = b28.anchor_here()
check(p["last_error"] and "管制區" in p["last_error"], "沒進管制區不能對齊", p["last_error"])
to_shopping(b28)
b28.on_pose(5000.0, 5000.0, 90.0, 1.0)
b28.on_gate_crossing("entering")
b28.on_pose(5000.0, 5000.0, 90.0, 1.1)
check(b28.get_state()["cart_pos"]["status"] == "rough", "進場後：約略位置")
b28.on_pose(5000.0, 6650.0, 90.0, 4.0)
b28.on_anchor_seen(sv.AnchorSighting(forward_m=1.0, lateral_m=0.0, height_m=0.5))
p = b28.get_state()
check(p["cart_pos"] == {"x": 0.0, "y": 2.25, "status": "anchored"} and "定位點" in (p["notice"] or ""),
      "看到前方 1 m 的地上標記 → 推車在定位點後 2 格，對齊並提示", (p["cart_pos"], p["notice"]))
b28.on_pose(5000.0, 7150.0, 90.0, 4.5)              # 往前推 0.5 m，標記剩前方 0.5 m、偏右 0.1 m
b28.on_anchor_seen(sv.AnchorSighting(forward_m=0.5, lateral_m=0.1, height_m=0.5))
check(b28.get_state()["cart_pos"] == {"x": -0.2, "y": 3.25, "status": "anchored"},
      "越近越準：每次看到都重新對齊（標記偏右 = 推車偏左）", b28.get_state()["cart_pos"])
b28.on_pose(5000.0, 7650.0, 90.0, 5.0)
check(b28.get_state()["cart_pos"] == {"x": -0.2, "y": 4.25, "status": "anchored"}, "再往前 0.5 m 正好到定位點",
      b28.get_state()["cart_pos"])
check(b28.get_state()["notice"] is None, "已經對齊了，再看到不重複提示")
b28.on_pose(5000.0 - 1000.0, 7650.0, 90.0, 6.0)
check(b28.get_state()["cart_pos"] == {"x": -2.2, "y": 4.25, "status": "anchored"}, "往左 1 m = 往左 2 格（unit_m 0.5）",
      b28.get_state()["cart_pos"])
b28.on_pose(4000.0, 7650.0, 93.0, 6.1)              # IMU 漂了 3°
b28.on_vp_heading(0.0)                              # 消失點在正中間：其實正對座標軸
check(abs(b28._frame.heading(93.0) - 90.0) < 3 * 0.8 + 1e-6 and b28._frame.heading(93.0) < 93.0,
      "消失點把航向往 90° 拉（w = 0.2）", b28._frame.heading(93.0))
b28.on_gate_crossing("exiting")
check(b28.get_state()["cart_pos"] == {"status": "none"}, "走出門口：位置作廢")
b28.on_gate_crossing("entering")
b28.on_pose(4000.0, 6650.0, 90.0, 7.0)
p = b28.anchor_here()
check(p["cart_pos"]["status"] == "anchored" and p["cart_pos"]["x"] == 0.0, "按「我在定位點」手動對齊", p["cart_pos"])
b28.force_logout()
check(b28.get_state()["cart_pos"] == {"status": "none"}, "強制登出：位置作廢")

print("\n[31] 相機執行緒：一張影像 → 定位點與消失點交給 Bridge")
import threading as _th  # noqa: E402
from ui.app_gui import _VISION_DEFAULTS, _VisionConsumer  # noqa: E402
scene = render_scene(30, gamma_deg=6.0, marker=(1.0, 0.1, 0))   # 貨架 + 地上的定位點標記


class _FakeCam:                                           # 測試用：固定回傳同一張影像
    K = K_

    def read_gray(self):
        return scene


class _Rec:
    def __init__(self):
        self.anchor, self.vp = [], []
        self.done = _th.Event()

    def on_anchor_seen(self, d):
        self.anchor.append(d)

    def on_vp_heading(self, delta):
        self.vp.append(delta)
        self.done.set()


rec, stop = _Rec(), _th.Event()
vc = _VisionConsumer(rec, _FakeCam(), sv.AnchorDetector(0, 0.15, K_), dict(_VISION_DEFAULTS, fps=20), stop)
vc.start()
rec.done.wait(5)
stop.set()
vc.join(2)
check(rec.anchor and abs(rec.anchor[0].forward_m - 1.0) < 0.05, "看到地上標記（前方 1 m）→ on_anchor_seen", rec.anchor[:1])
check(rec.vp and abs(rec.vp[0] - 6.0) < 1.0, "貨架消失點 → on_vp_heading(δ ≈ 6°)", rec.vp[:1])

# ======================================================================
print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("全部通過")
