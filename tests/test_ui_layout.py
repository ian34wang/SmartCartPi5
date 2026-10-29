"""14 頁版面與前端互動（Playwright 版面/互動測試，選用）

這支不需要任何硬體，但需要安裝 playwright 與一份 chromium：
    pip install playwright && playwright install chromium

跑法（在專案根目錄）：
    python3 tests/test_ui_layout.py

如果環境裡沒有 playwright，跳過這支即可——tests/test_app_gui_bridge.py
（純 Python、不需要瀏覽器）才是必跑的那支。
"""
import asyncio, sys
from pathlib import Path
from playwright.async_api import async_playwright

HTML = (Path(__file__).resolve().parent.parent / "ui" / "templates" / "index.html").as_uri()
OUT = Path(__file__).resolve().parent
SCREENS = ["main", "login", "loading_profile", "allergens", "diet", "religion", "budget",
           "shopping", "navigation", "promotions", "profile", "weight_alert", "checkout", "exit_confirm"]

# 假的 pywebview api：記錄前端呼叫了什麼，並回一個合理的 payload 讓 render() 跑得動
STUB = """
window.__calls = [];
function payload(over) {
  return Object.assign({
    screen: "shopping", last_error: null, member_id: "MEMBER-0001", member_name: "測試會員 A",
    wizard_profile: {allergens: [], diet: null, religion: []}, budget: 1000,
    cart_items: [{name:"統一陽光豆漿 300ml", quantity:1, subtotal:25}],
    cart_count: 1, cart_total: 25, pending_item_barcode: null, pending_item_mode: null,
    unscanned_baseline_g: null, current_weight_g: 320.0, sensor_connected: true,
    sm_state: "shopping", exit_countdown: null,
    has_weight_reading: true, last_scan: null, gate_events: 0, awaiting_gate_entry: false,
    notice: null, staff_pending: null, assist_open: null, is_guest: false,
    saved_profile: {exists: false}, recent_transactions: []
  }, over || {});
}
window.pywebview = { api: new Proxy({}, { get: function (t, name) {
  return function () {
    var args = Array.prototype.slice.call(arguments);
    window.__calls.push({name: String(name), args: args});
    var over = {};
    if (name === "on_scan_input") over = {last_scan: args[0]};
    return Promise.resolve(payload(over));
  };
}})};
"""


async def main():
    failures = []
    async with async_playwright() as pw:
        # 開發沙盒裡的 chromium 放在固定路徑；一般環境用 playwright 自己裝的那份。
        import os
        _exe = "/opt/pw-browsers/chromium"
        _launch = {"executable_path": _exe} if os.path.exists(_exe) else {}
        browser = await pw.chromium.launch(**_launch)
        # 故意用比設計稿 1280 矮的視窗，重現實機上底部導覽列被裁掉的情境
        page = await browser.new_page(viewport={"width": 720, "height": 1180})
        await page.add_init_script(STUB)
        await page.goto(HTML)
        await page.wait_for_timeout(800)

        for sid in SCREENS:
            await page.evaluate(
                "sid => { document.querySelectorAll('.screen').forEach(e => "
                "e.classList.toggle('active', e.id === 'screen-' + sid)); }", sid)
            await page.wait_for_timeout(120)
            m = await page.evaluate("""sid => {
                const el = document.getElementById('screen-' + sid);
                const cs = getComputedStyle(el);
                return {dir: cs.flexDirection, h: el.clientHeight, sh: el.scrollHeight,
                        bodyOverflow: document.body.scrollWidth > document.body.clientWidth};
            }""", sid)
            ok_dir = m["dir"] == "column"
            ok_fit = m["sh"] <= m["h"] + 1
            if not ok_dir:
                failures.append(f"{sid}: flex-direction={m['dir']}（應為 column）")
            if not ok_fit:
                failures.append(f"{sid}: 內容溢出 scrollHeight={m['sh']} > clientHeight={m['h']}")
            if m["bodyOverflow"]:
                failures.append(f"{sid}: 水平溢出")
            print(f"  {sid:<16} dir={m['dir']:<8} {m['sh']}/{m['h']}  {'OK' if ok_dir and ok_fit else 'FAIL'}")

        # 底部導覽列要真的在可視範圍內
        await page.evaluate("document.querySelectorAll('.screen').forEach(e => "
                            "e.classList.toggle('active', e.id === 'screen-shopping'))")
        nav = await page.evaluate("""() => {
            const el = document.querySelector('#screen-shopping .bottomnav');
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {top: r.top, bottom: r.bottom, vh: window.innerHeight};
        }""")
        if nav is None:
            failures.append("找不到購物畫面的底部導覽列")
        else:
            print(f"  bottomnav bottom={nav['bottom']:.0f} viewport={nav['vh']}")
            if nav["bottom"] > nav["vh"] + 1:
                failures.append(f"底部導覽列被裁掉（bottom={nav['bottom']:.0f} > {nav['vh']}）")
        await page.screenshot(path=str(OUT / "shot_shopping.png"))

        # 感測器燈號要跟著 payload 變
        await page.evaluate("""() => window.APP.render({
            screen:'shopping', last_error:null, member_id:'X', member_name:'X',
            wizard_profile:{allergens:[],diet:null,religion:[]}, budget:null,
            cart_items:[], cart_count:0, cart_total:0, pending_item_barcode:null,
            pending_item_mode:null, unscanned_baseline_g:null, current_weight_g:null,
            sensor_connected:true, sm_state:'shopping', exit_countdown:null,
            has_weight_reading:false, last_scan:null, gate_events:0, awaiting_gate_entry:false })""")
        txt = await page.text_content("#f-sensor-text")
        if txt.strip() == "無秤重資料":
            print("  感測器燈號跟著 payload 變  OK")
        else:
            failures.append(f"感測器燈號錯誤：text={txt!r}")

        # ------------------------------------------------------------------
        # 2026-10-28 新增的按鈕都要真的呼叫到 Python（不是跳「尚未實作」）
        async def calls_after(js):
            await page.evaluate("window.__calls = []")
            await page.evaluate(js)
            await page.wait_for_timeout(150)
            return await page.evaluate("window.__calls")

        def has(calls, name, *args):
            return any(c["name"] == name and list(c["args"][:len(args)]) == list(args) for c in calls)

        await page.evaluate("window.pywebview.api.get_state().then(window.APP.render)")
        await page.wait_for_timeout(150)
        await page.click("#screen-shopping .navitem[data-tab='profile']")
        await page.wait_for_timeout(150)
        active = await page.evaluate("document.querySelector('.screen.active').id")
        if active != "screen-profile":
            failures.append(f"點「我的」沒有切到我的分頁：{active}")
        else:
            print("  「我的」分頁可以切換  OK")
        await page.screenshot(path=str(OUT / "shot_profile.png"))
        checks = [
            ("document.querySelector(\"#screen-profile .opt[data-value='1500']\").click()",
             ("set_session_budget", "1500"), "我的：預算選項"),
            ("document.querySelector('#screen-profile .btn-outline').click()", ("edit_preferences",), "我的：重新設定偏好"),
            ("document.querySelector('#screen-checkout .back-btn').click()", ("unlock_checkout",), "結帳：返回繼續選購"),
            ("document.querySelector('#screen-checkout .btn-primary').click()", ("confirm_payment", "LINE Pay"),
             "結帳：確認付款帶付款方式"),
            ("document.querySelectorAll('#screen-weight_alert .btn-dashed')[0].click()", ("request_rezero",),
             "秤重異常：重新校準歸零"),
            ("document.querySelectorAll('#screen-weight_alert .btn-dashed')[1].click()",
             ("request_assistance", "weight_alert"), "秤重異常：呼叫店員"),
            ("document.querySelector('#screen-exit_confirm .btn-dashed').click()",
             ("request_assistance", "exit_confirm"), "離場確認：需要協助"),
        ]
        for js, want, label in checks:
            calls = await calls_after(js)
            if has(calls, *want):
                print(f"  {label}  OK")
            else:
                failures.append(f"{label} 沒有呼叫 {want}：{calls}")

        # 載入畫面依已儲存的設定顯示；協助橫幅；預算預選
        await page.evaluate("""() => window.APP.render(Object.assign({}, {
            screen:'loading_profile', last_error:null, member_id:'MEMBER-0001', member_name:'測試會員 A',
            wizard_profile:{allergens:[],diet:null,religion:[]}, budget:null, cart_items:[], cart_count:0,
            cart_total:0, pending_item_barcode:null, pending_item_mode:null, unscanned_baseline_g:null,
            current_weight_g:null, sensor_connected:true, sm_state:'logged_in_outside_zone', exit_countdown:null,
            has_weight_reading:false, last_scan:null, gate_events:0, awaiting_gate_entry:false, notice:null,
            staff_pending:null, is_guest:false, recent_transactions:[],
            assist_open:{reason:'呼叫店員協助', time:'10/28 21:00'},
            saved_profile:{exists:true, allergens:['花生'], diet:'蛋奶素', religion:[], budget_text:'$500',
                           updated:'10/28 20:00'} }))""")
        cl = await page.text_content("#f-loading-checklist")
        banner_hidden = await page.evaluate("document.getElementById('f-assist-banner').hidden")
        if "花生" in cl and "蛋奶素" in cl and "$500" in cl:
            print("  載入畫面顯示已儲存的設定  OK")
        else:
            failures.append(f"載入畫面清單錯誤：{cl!r}")
        if not banner_hidden:
            print("  待處理的協助請求顯示橫幅  OK")
        else:
            failures.append("有待處理的協助請求，但橫幅沒有顯示")
        await page.screenshot(path=str(OUT / "shot_loading_profile.png"))
        await page.evaluate("""() => window.APP.render(Object.assign({}, {
            screen:'budget', last_error:null, member_id:'MEMBER-0001', member_name:'A',
            wizard_profile:{allergens:[],diet:null,religion:[]}, budget:500, cart_items:[], cart_count:0,
            cart_total:0, sensor_connected:true, has_weight_reading:false, gate_events:0, sm_state:'x',
            saved_profile:{exists:false}, recent_transactions:[] }))""")
        sel = await page.evaluate("Array.from(document.querySelectorAll('#screen-budget .opt-selected'))"
                                  ".map(e => e.dataset.value)")
        if sel == ["500"]:
            print("  預算畫面預先選好已儲存的預算  OK")
        else:
            failures.append(f"預算預選錯誤：{sel}")

        await browser.close()

    print("\n" + "=" * 50)
    if failures:
        for f in failures:
            print("FAIL:", f)
        sys.exit(1)
    print("版面與前端互動全部通過")


asyncio.run(main())
