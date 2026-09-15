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


def new_bridge():
    db = DBManager(tempfile.mktemp(suffix=".db"))
    db.init_db(seed=True)   # 測試需要那 5 筆測試商品
    sm = CartStateMachine(db=db, cart=CartManager(), config=load_state_machine_config())
    return Bridge(db=db, sm=sm, window_holder={"window": None})


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
check(p["screen"] == "allergens", "選 A 仍走精靈", p["screen"])
check(bool(p["last_error"]), "有說明「資料庫還沒有欄位可存設定」")
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

# ======================================================================
print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("全部通過")
