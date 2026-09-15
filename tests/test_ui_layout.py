"""13 頁版面與前端互動（Playwright 版面/互動測試，選用）

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

HTML = (Path(__file__).resolve().parent.parent / "ui" / "templates" / "index.html").as_uri()
OUT = Path(__file__).resolve().parent
SCREENS = ["main", "login", "loading_profile", "allergens", "diet", "religion", "budget",
           "shopping", "navigation", "promotions", "weight_alert", "checkout", "exit_confirm"]

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
    has_weight_reading: true, last_scan: null, gate_events: 0, awaiting_gate_entry: false
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

        await browser.close()

    print("\n" + "=" * 50)
    if failures:
        for f in failures:
            print("FAIL:", f)
        sys.exit(1)
    print("版面與前端互動全部通過")


asyncio.run(main())
