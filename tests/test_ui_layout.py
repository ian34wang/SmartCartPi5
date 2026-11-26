"""14 頁版面與前端互動（Playwright 版面/互動測試，選用）

這支不需要任何硬體，但需要安裝 playwright 與一份 chromium：
    pip install playwright && playwright install chromium

跑法（在專案根目錄）：
    python3 tests/test_ui_layout.py

如果環境裡沒有 playwright，跳過這支即可——tests/test_app_gui_bridge.py
（純 Python、不需要瀏覽器）才是必跑的那支。
"""
import asyncio, json, sys
from pathlib import Path
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.store import layout as store_layout  # noqa: E402

HTML = (Path(__file__).resolve().parent.parent / "ui" / "templates" / "index.html").as_uri()
OUT = Path(__file__).resolve().parent
SCREENS = ["main", "login", "loading_profile", "allergens", "diet", "religion", "budget",
           "shopping", "navigation", "promotions", "profile", "weight_alert", "checkout", "exit_confirm"]

_LAY = store_layout.load()


def _nav(code):
    """跟 ui/bridge.Bridge._catalog_payload 一樣的找貨欄位。"""
    return json.dumps({"shelf": code, "shelf_text": store_layout.describe(_LAY, code),
                       "instruction": store_layout.instruction(_LAY, code),
                       "route": [list(q) for q in store_layout.route(_LAY, code)],
                       "spot": list(store_layout.slot_point(_LAY, code))}, ensure_ascii=False)


# 假的 pywebview api：記錄前端呼叫了什麼，並回一個合理的 payload 讓 render() 跑得動
STUB = ("var STORE = " + json.dumps(store_layout.to_client(_LAY), ensure_ascii=False) + ";\n"
        + "var NAV_L4 = " + _nav("L4") + ";\nvar NAV_R2 = " + _nav("R2") + ";\n") + """
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
    saved_profile: {exists: false}, recent_transactions: [],
    cart_list_total: 25, budget_remaining: 975, pending_item: null,
    catalog: [
      Object.assign({barcode:"4710018001234", name:"統一陽光豆漿 300ml", price:25, list_price:25, discount_pct:0, in_cart:true, health:""}, NAV_L4),
      Object.assign({barcode:"PEANUT", name:"花生醬", price:80, list_price:80, discount_pct:0, in_cart:false, health:"含花生（過敏原：花生）"}, NAV_R2),
      {barcode:"NOSHELF", name:"沒位置的", price:10, list_price:10, discount_pct:0, shelf:"", shelf_text:"", instruction:"", route:[], spot:null, in_cart:false, health:""}
    ],
    promotions: [
      {kind:"discount", barcode:"SALAD", title:"特價 即期沙拉", body:"原價 $60 → $30", health:"", can_nav:false},
      {kind:"pick", barcode:"PEANUT", title:"適合你：花生醬", body:"$80", health:"", can_nav:true}
    ],
    store: STORE, cart_pos: {status: "none"}
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

        # ------------------------------------------------------------------
        # 2026-11-23：找貨（店面平面圖）、優惠、比對中商品的價格與健康護照
        await page.evaluate("window.pywebview.api.get_state().then(window.APP.render)")
        await page.wait_for_timeout(150)
        await page.click("#screen-shopping .navitem[data-tab='navigation']")
        await page.wait_for_timeout(150)
        shelves = await page.evaluate("document.querySelectorAll('#f-nav-map svg rect').length")
        chips = await page.evaluate("document.querySelectorAll('#f-nav-list .nav-chip').length")
        if shelves == 3 and chips == 2:      # 2 座貨架 + 定位點的菱形
            print("  找貨：平面圖畫出 2 座貨架與定位點、只列有貨架位置的商品  OK")
        else:
            failures.append(f"找貨畫面不對：rect={shelves} chips={chips}")
        await page.click("#f-nav-list .nav-chip:nth-child(2)")
        await page.wait_for_timeout(150)
        route = await page.evaluate("(document.querySelector('#f-nav-map polyline')||{getAttribute:()=>''}).getAttribute('points')")
        info = await page.text_content("#f-nav-info")
        if route and len(route.split()) >= 3 and "右架・右側・後段" in info and "在貨架右側" in info and "健康護照" in info:
            print("  找貨：點商品 → 畫出路線（右架右側）、說明文字、健康護照  OK")
        else:
            failures.append(f"找貨目標不對：{route!r} {info!r}")
        await page.evaluate("window.APP.setPos({x: 0, y: 2.2, status: 'anchored', heading: 90})")
        dot = await page.evaluate("""() => { const d = document.getElementById('f-cart-dot');
            const m = /translate\\(([-\\d.]+),([-\\d.]+)\\)/.exec(d.getAttribute('transform') || '');
            return [d.getAttribute('visibility'), m && m[1], m && m[2], d.getAttribute('transform'),
                    document.getElementById('f-cart-arrow').getAttribute('visibility')]; }""")
        anchor = await page.evaluate("""() => { const t = Array.from(document.querySelectorAll('#f-nav-map text'))
            .find(e => e.textContent === '#0 定位點'); return t ? [+t.getAttribute('x'), +t.getAttribute('y')] : null; }""")
        pos_txt = await page.text_content("#f-nav-pos")
        if dot[0] == "visible" and anchor and abs(float(dot[1]) + 16 - anchor[0]) < 1 and "(0.00, 2.20)" in pos_txt and "航向 90°" in pos_txt:
            print("  找貨：APP.setPos 把推車點移到定位點  OK")
        else:
            failures.append(f"推車點不對：{dot} {anchor} {pos_txt!r}")
        if dot[3] and "rotate(-90)" in dot[3] and dot[4] == "visible":
            print("  找貨：推車圖示有朝向箭頭（朝店內 = 畫面往上）  OK")
        else:
            failures.append(f"推車朝向不對：{dot}")
        await page.evaluate("window.APP.setPos({x: 0.5, y: 2.8, status: 'anchored', heading: 0, route: [[0.5, 2.8], [1.47, 2.8], [1.47, 3.98]]})")
        live = await page.evaluate("""() => [document.getElementById('f-route').getAttribute('points').split(' ').length,
            document.getElementById('f-cart-dot').getAttribute('transform')]""")
        if live[0] == 3 and "rotate(0)" in live[1]:
            print("  找貨：有推車位置時路線改成從推車出發、箭頭跟著轉  OK")
        else:
            failures.append(f"即時路線不對：{live}")
        await page.evaluate("window.APP.setPos({x: 0.6, y: 2.75, status: 'anchored', heading: 30, route: [[0.6, 2.75], [1.415, 2.825], [1.47, 3.98]]})")
        await page.screenshot(path=str(OUT / "shot_navigation_cart.png"))
        await page.evaluate("window.APP.setPos({x: 0, y: 2.2, status: 'anchored', heading: 90})")
        await page.click("#screen-navigation .nav-anchor-btn")
        await page.wait_for_timeout(100)
        called = await page.evaluate("window.__calls.map(c => c.name)")
        if "anchor_here" in called:
            print("  找貨：「我在定位點」呼叫 anchor_here  OK")
        else:
            failures.append("「我在定位點」沒有呼叫 anchor_here")
        await page.click("#f-nav-list .nav-chip:nth-child(2)")       # 再點一次同一個商品 = 取消導航
        await page.wait_for_timeout(150)
        cancel = await page.evaluate("""() => [window.__calls.filter(c => c.name === 'set_nav_target').map(c => c.args[0]),
            !!document.querySelector('#f-nav-map polyline'), document.querySelectorAll('#f-nav-list .nav-chip.selected').length]""")
        if cancel[0][-2:] == ["PEANUT", ""] and not cancel[1] and cancel[2] == 0:
            print("  找貨：再點一次同一個商品 → 取消導航（路線消失）  OK")
        else:
            failures.append(f"再點一次沒有取消導航：{cancel}")
        await page.click("#f-nav-list .nav-chip:nth-child(2)")       # 恢復導航，後面截圖用
        await page.wait_for_timeout(150)
        await page.screenshot(path=str(OUT / "shot_navigation.png"))
        await page.click("#screen-navigation .navitem[data-tab='promotions']")
        await page.wait_for_timeout(150)
        n_cards = await page.evaluate("document.querySelectorAll('#f-promo-cards .card').length")
        n_cta = await page.evaluate("document.querySelectorAll('#f-promo-cards .card-cta').length")
        if n_cards == 2 and n_cta == 1:
            print("  優惠：卡片由 payload 產生，可導航的才有按鈕  OK")
        else:
            failures.append(f"優惠卡片不對：{n_cards} 張、{n_cta} 個按鈕")
        await page.screenshot(path=str(OUT / "shot_promotions.png"))
        await page.click("#f-promo-cards .card-cta")
        await page.wait_for_timeout(150)
        active = await page.evaluate("document.querySelector('.screen.active').id")
        if active == "screen-navigation":
            print("  優惠：導航去貨架 → 切到找貨  OK")
        else:
            failures.append(f"導航按鈕沒切到找貨：{active}")
        await page.evaluate("""() => window.pywebview.api.get_state().then(p => { p.screen = 'shopping';
            p.pending_item_barcode = 'PEANUT'; p.pending_item_mode = 'add';
            p.pending_item = {barcode:'PEANUT', name:'花生醬', price:40, list_price:80, total_after:1065,
                              remaining_after:-65, health:'含花生（過敏原：花生）'};
            p.budget = 1000; p.cart_total = 1025; window.APP.showTab('shopping'); window.APP.render(p); })""")
        await page.wait_for_timeout(200)
        box = await page.text_content("#f-pending-box-wrap")
        pct = await page.text_content("#f-progress-pct")
        if "花生醬" in box and "超出預算" in box and "健康護照" in box and "超出預算" in pct:
            print("  購物：比對中顯示名稱、價格、預算與健康護照；進度條顯示超出  OK")
        else:
            failures.append(f"比對中區塊不對：{box!r} / {pct!r}")
        await page.screenshot(path=str(OUT / "shot_shopping_pending.png"))

        # 定位實測模式（app_gui --nav-only）：直接是找貨頁、沒有「關閉導航」，位置文字有座標、航向、最近的標記
        await page.evaluate("""() => window.pywebview.api.get_state().then(p => { p.screen = 'shopping';
            p.pending_item_barcode = null; p.nav_only = true;
            p.cart_pos = {x: 0.02, y: 3.74, status: 'anchored', heading: 90, marker: {id: 4, name: '後走道交叉口', age_s: 2.4}};
            window.APP.showTab('shopping'); window.APP.render(p); })""")
        await page.wait_for_timeout(200)
        nav = await page.evaluate("""() => [document.querySelector('.screen.active').id,
            getComputedStyle(document.querySelector('#screen-navigation .close-btn')).display
              + getComputedStyle(document.querySelector('#screen-navigation .bottomnav')).display,
            document.getElementById('f-nav-pos').textContent]""")
        if nav[0] == "screen-navigation" and nav[1] == "nonenone" and "(0.02, 3.74)" in nav[2] and "航向 90°" in nav[2] and "#4" in nav[2]:
            print("  定位實測模式：直接找貨頁、沒有關閉導航、顯示座標／航向／標記  OK")
        else:
            failures.append(f"定位實測模式不對：{nav}")
        await page.screenshot(path=str(OUT / "shot_nav_only.png"))
        await browser.close()

    print("\n" + "=" * 50)
    if failures:
        for f in failures:
            print("FAIL:", f)
        sys.exit(1)
    print("版面與前端互動全部通過")


asyncio.run(main())
