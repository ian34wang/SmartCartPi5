"""tools/build_app_ui.py

組裝腳本：把 design-cart-ui/*.dc.html（畫布用的視覺稿，字級已經是使用者確認
過的版本）轉成 ui/templates/index.html——一個真的可以跟 CartStateMachine/
DBManager 互動的單頁應用（SPA），而不是靜態範例資料。

用法：
    python3 tools/build_app_ui.py

重要：`ui/templates/index.html` 是這支腳本「產生」出來的，不要直接手改那個
檔案——下次重跑就會被蓋掉。所有前端行為（JS）、共用樣式、開發面板的修改都要
改在這支腳本的 TEMPLATE 字串裡；各畫面的視覺樣式則改 design-cart-ui/*.dc.html
之後重跑本腳本。

做法：
  1. 每個畫面的 <style> 內容，選擇器全部加上 #screen-<id> 前綴（body 規則變
     成該畫面容器本身的樣式），避免 13 份原本各自獨立的 CSS 混在同一頁時互
     相污染 class 名稱。
  2. 每個畫面的內容，做少量針對性補丁：把原本示範用的假資料容器換成有 id
     的空容器（交給 JS render() 動態填）、把按鈕/選項加上 onclick 呼叫
     Python 後端（pywebview js_api）。
  3. 組合成一份 index.html，外加共用的頂部退出/開發工具列、底部導覽事件代
     理、render() 邏輯。

本檔案裡的「補丁」(PATCHERS) 是跟目前畫面結構綁定的，如果之後線框稿的 HTML
結構整個改了（不只是數值調整），補丁要跟著更新——補丁找不到目標字串時大多會
安靜地什麼都不做（`str.replace` 的特性），所以改完一定要實際跑一次、開畫面確
認，不要只看腳本沒報錯就當成功。
"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = _REPO_ROOT / "design-cart-ui"
OUT_PATH = _REPO_ROOT / "ui" / "templates" / "index.html"

SCREENS = [
    ("Main.dc.html", "main"),
    ("Login.dc.html", "login"),
    ("LoadingProfile.dc.html", "loading_profile"),
    ("Allergens.dc.html", "allergens"),
    ("Diet.dc.html", "diet"),
    ("Religion.dc.html", "religion"),
    ("Budget.dc.html", "budget"),
    ("Shopping.dc.html", "shopping"),
    ("Navigation.dc.html", "navigation"),
    ("Promotions.dc.html", "promotions"),
    ("WeightAlert.dc.html", "weight_alert"),
    ("Checkout.dc.html", "checkout"),
    ("ExitConfirm.dc.html", "exit_confirm"),
]


def extract(fname):
    raw = (SRC_DIR / fname).read_text(encoding="utf-8")
    style = re.search(r"<style>(.*?)</style>", raw, re.S).group(1)
    body = re.search(r"</helmet>(.*?)</x-dc>", raw, re.S).group(1).strip()
    return style, body


def scope_css(css_text, screen_id):
    # 原本用「一行一條規則」的假設逐行掃描，但來源檔案裡 body{...} 是跨多行
    # 寫的（margin/width/height/... 各佔一行），逐行掃描下每一行都對不上
    # `selector { decls }` 這個單行 pattern，導致整條 body 規則（含
    # flex-direction:column、背景色、外框）被靜靜地丟掉——唯一的痕跡是排版
    # 變成預設的橫向 flex，画面看起來從左到右而不是由上到下。改成先用
    # DOTALL 抓出完整的 `selector { decls }` 區塊（decls 本身允許跨行），
    # 不再依賴「規則要在同一行」這個原本就不成立的假設。
    out = []
    css_text = re.sub(r"@import[^;]*;", "", css_text)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css_text, re.S):
        selectors, decls = m.group(1).strip(), m.group(2).strip()
        if not selectors:
            continue
        parts = [s.strip() for s in selectors.split(",")]
        if parts == ["body"]:
            decls2 = re.sub(r"display\s*:\s*flex\s*;?", "", decls)
            out.append(f"#screen-{screen_id} {{ {decls2} }}")
            continue
        if parts == ["*"]:
            continue
        scoped = ", ".join(f"#screen-{screen_id} {p}" for p in parts)
        out.append(f"{scoped} {{ {decls} }}")
    return "\n".join(out)


def patch_nth_open_tags(html, tag_pattern, injections):
    it = iter(injections)

    def repl(m):
        tag = m.group(0)
        try:
            inject = next(it)
        except StopIteration:
            return tag
        return tag[:-1] + " " + inject + ">"

    return re.sub(tag_pattern, repl, html)


def remove_balanced_div(html, start_marker, count=1):
    """移除從 `start_marker`（例如 '<div class="pending-box">'）開始、一路配
    對到它自己真正的結束 `</div>` 為止的整段內容（含頭尾標籤本身），用「數
    開合標籤層數」而不是固定寫死「後面跟著幾個 `</div>`」——原本
    `patch_shopping()` 是用 `r'...</div>\s*</div>\s*</div>'` 這種寫死三層
    的 regex 來抓 pending-box 的結尾，但 pending-box 裡面的巢狀 div 層數其
    實是四層（一個沒有 class 的 plain <div> 包在 warn-box 裡面），少算一層
    導致連 pending-box 外面那層 .section 的結束標籤都被一起吃掉、後面所有
    畫面的 DOM 巢狀從此全部錯位一層（視覺上的症狀是底部導覽列「消失」，
    其實是整個 flex 容器巢狀被打亂，不是真的不見）。這支函式改成真的照標
    籤配對算層數，不管裡面巢狀幾層都能抓對真正的結尾。
    """
    for _ in range(count):
        start = html.find(start_marker)
        if start == -1:
            raise ValueError(f"remove_balanced_div: 找不到起始標記 {start_marker!r}")
        pos = start + len(start_marker)
        depth = 1  # start_marker 本身就是一個還沒關閉的 <div ...>
        while depth > 0:
            next_open = html.find("<div", pos)
            next_close = html.find("</div>", pos)
            if next_close == -1:
                raise ValueError("remove_balanced_div: 標籤沒有配對完整，找不到足夠的 </div>")
            if next_open != -1 and next_open < next_close:
                depth += 1
                pos = next_open + len("<div")
            else:
                depth -= 1
                pos = next_close + len("</div>")
        html = html[:start] + html[pos:]
    return html


# ----------------------------------------------------------------------
# 每個畫面的針對性補丁
# ----------------------------------------------------------------------
def patch_main(body):
    body = body.replace(
        '<button class="btn-big">',
        '<button class="btn-big" onclick="APP.callApi(\'go_to_login\')">',
    )
    return body


def patch_login(body):
    body = body.replace(
        '<button class="btn-dashed">以訪客身份繼續（暫不登入）</button>',
        '<button class="btn-dashed" onclick="APP.callApi(\'continue_as_guest\')">以訪客身份繼續（暫不登入）</button>',
    )
    # 原本這裡有一個「手動輸入會員代碼」的開發測試輸入框。拿掉了：登入的唯一
    # 路徑就是刷實體會員條碼（evdev 直接收，見 drivers/barcode_scanner.py），
    # 沒有會員卡的人走下面既有的「以訪客身份繼續」。留一個打字入口等於又多一
    # 條旁路，而且它會在掃描器其實沒接上的時候讓畫面看起來一切正常。
    err_block = '<div id="f-login-error" style="color:#b00020;font-size:16px;min-height:22px;"></div>'
    body = body.replace('<div class="divider-text">或</div>', err_block + '<div class="divider-text">或</div>')
    return body


def patch_loading_profile(body):
    body = re.sub(r'\s*<span class="badge-new">.*?</span>', "", body, flags=re.S)
    body = body.replace(
        "歡迎回來，王小明",
        '歡迎回來，<span id="f-loading-member-name">會員</span>',
    )
    body = body.replace(
        '<div class="branch-cta">確認並開始購物 →</div>',
        '<div class="branch-cta" onclick="APP.callApi(\'loading_profile_choice\',\'A\')">確認並開始購物 →</div>',
    )
    body = body.replace(
        '<div class="branch-cta">開始設定 →</div>',
        '<div class="branch-cta" onclick="APP.callApi(\'loading_profile_choice\',\'B\')">開始設定 →</div>',
    )
    return body


def patch_allergens(body):
    names = ["堅果類", "甲殼類", "芒果", "花生", "牛奶／羊奶", "蛋", "芝麻", "含麩質穀物", "大豆", "魚類", "亞硫酸鹽", "都沒有"]
    injs = [f"data-name=\"{n}\" onclick=\"APP.wizardToggle('allergens','{n}')\"" for n in names]
    body = patch_nth_open_tags(body, r'<div class="chip( selected)?">', injs)
    body = body.replace(
        '<div class="skip-link">跳過此步驟</div>',
        '<div class="skip-link" onclick="APP.callApi(\'wizard_goto\',\'diet\')">跳過此步驟</div>',
    )
    body = body.replace(
        '<div class="btn">上一步</div>',
        '<div class="btn" onclick="APP.callApi(\'wizard_goto\',\'login\')">上一步</div>',
    )
    body = body.replace(
        '<div class="btn btn-primary">下一步：葷素習慣 →</div>',
        '<div class="btn btn-primary" onclick="APP.callApi(\'wizard_goto\',\'diet\')">下一步：葷素習慣 →</div>',
    )
    return body


def patch_diet(body):
    values = ["無特別素食習慣", "全素（純素）", "蛋奶素", "奶素", "蛋素", "五辛素（植物五辛素）", "鍋邊素（可接受海鮮／肉湯）"]
    injs = [f"data-value=\"{v}\" onclick=\"APP.wizardSetDiet('{v}')\"" for v in values]
    body = patch_nth_open_tags(body, r'<div class="radio-row( selected)?">', injs)
    body = body.replace(
        '<div class="skip-link">跳過此步驟</div>',
        '<div class="skip-link" onclick="APP.callApi(\'wizard_goto\',\'religion\')">跳過此步驟</div>',
    )
    body = body.replace(
        '<div class="btn">上一步</div>',
        '<div class="btn" onclick="APP.callApi(\'wizard_goto\',\'allergens\')">上一步</div>',
    )
    body = body.replace(
        '<div class="btn btn-primary">下一步：宗教飲食 →</div>',
        '<div class="btn btn-primary" onclick="APP.callApi(\'wizard_goto\',\'religion\')">下一步：宗教飲食 →</div>',
    )
    return body


def patch_religion(body):
    names = ["無特別宗教飲食規範", "伊斯蘭教（清真／Halal）", "印度教", "台灣民俗不食牛"]
    injs = [f"data-name=\"{n}\" onclick=\"APP.wizardToggle('religion','{n}')\"" for n in names]
    body = patch_nth_open_tags(body, r'<div class="chip-row( selected)?">', injs)
    body = body.replace(
        '<div class="skip-link">跳過此步驟</div>',
        '<div class="skip-link" onclick="APP.callApi(\'wizard_goto\',\'budget\')">跳過此步驟</div>',
    )
    body = body.replace(
        '<div class="btn">上一步</div>',
        '<div class="btn" onclick="APP.callApi(\'wizard_goto\',\'diet\')">上一步</div>',
    )
    body = body.replace(
        '<div class="btn btn-primary">完成，下一步：本次預算 →</div>',
        '<div class="btn btn-primary" onclick="APP.callApi(\'wizard_goto\',\'budget\')">完成，下一步：本次預算 →</div>',
    )
    return body


def patch_budget(body):
    values = ["500", "1000", "1500", "unlimited"]
    injs = [f"data-value=\"{v}\" onclick=\"APP.budgetPick('{v}')\"" for v in values]
    body = patch_nth_open_tags(body, r'<div class="opt( opt-selected)?">', injs)
    body = body.replace(
        '<div class="custom-input">$ ____</div>',
        '<input id="f-budget-custom" class="custom-input" type="number" placeholder="輸入金額" '
        'style="border:2px dashed #1a1a1a;background:#fff;font:inherit;text-align:center;color:#1a1a1a;" '
        "oninput=\"APP.budgetPick(this.value)\">",
    )
    body = body.replace(
        '<div class="btn">上一步</div>',
        '<div class="btn" onclick="APP.callApi(\'wizard_goto\',\'religion\')">上一步</div>',
    )
    body = body.replace(
        '<div class="btn btn-primary">開始購物 →</div>',
        '<div class="btn btn-primary" onclick="APP.confirmBudget()">開始購物 →</div>',
    )
    return body


def patch_shopping(body):
    body = body.replace(
        '<span>王小明　總額 = $520 ／ 預算 = $1000</span>',
        '<span id="f-shop-header"></span>',
    )
    body = body.replace(
        '<div class="progress-fill"></div>',
        '<div class="progress-fill" id="f-progress-fill" style="width:0%;"></div>',
    )
    body = body.replace("<span>52% 已使用</span>", '<span id="f-progress-pct"></span>')
    body = body.replace("<span>購物車 = 3 件</span>", '<span id="f-cart-count-line"></span>')

    # 感測器狀態文字改成有 id 的空容器，交給 render() 依真實 payload 填——設計
    # 稿裡是寫死的「感測器正常」，實機上不管有沒有秤重資料都顯示正常，會蓋掉
    # 「掃碼被拒絕是因為根本沒有秤重讀數」這個最關鍵的線索。
    body = body.replace(
        "      感測器正常\n",
        '      <span id="f-sensor-text">感測器正常</span>\n',
    )

    body = body.replace(
        '<div class="hint-line">系統自動判斷：先掃碼再放入＝加入；先拿出再掃碼＝移除；拿取後忘記掃碼會逾時警示</div>',
        '<div class="hint-line">系統自動判斷：先掃碼再放入＝加入；先拿出再掃碼＝移除；拿取後忘記掃碼會逾時警示</div>\n'
        '<div id="f-pending-box-wrap"></div>',
    )
    # 移除整段原本示範用的 pending-box（改由 JS 依真實 session 動態產生，含/不含都可能）——
    # 用配對層數的方式抓真正的結尾，不要用寫死幾層 </div> 的 regex（原因見
    # remove_balanced_div() 的說明）。
    body = remove_balanced_div(body, '<div class="pending-box">')

    body = re.sub(
        r'<div class="item-row">.*?</div>\s*<div class="item-row">.*?</div>\s*<div class="item-row">.*?</div>',
        '<div id="f-cart-items"></div>',
        body,
        flags=re.S,
    )
    body = patch_nth_open_tags(
        body,
        r'<div class="navitem( active)?( nav-badge)?">',
        [
            'data-tab="shopping"',
            'data-tab="navigation"',
            'data-tab="promotions"',
            'data-tab="profile"',
            'data-tab="checkout"',
        ],
    )
    return body


def patch_navigation(body):
    body = body.replace(
        '<button class="close-btn">✕ 關閉導航</button>',
        '<button class="close-btn" onclick="APP.showTab(\'shopping\')">✕ 關閉導航</button>',
    )
    body = patch_nth_open_tags(
        body,
        r'<div class="navitem( active)?( nav-badge)?">',
        [
            'data-tab="shopping"',
            'data-tab="navigation"',
            'data-tab="promotions"',
            'data-tab="profile"',
            'data-tab="checkout"',
        ],
    )
    return body


def patch_promotions(body):
    body = body.replace(
        '<button class="back-btn">← 返回購物車</button>',
        '<button class="back-btn" onclick="APP.showTab(\'shopping\')">← 返回購物車</button>',
    )
    body = patch_nth_open_tags(
        body,
        r'<div class="navitem( active)?( nav-badge)?">',
        [
            'data-tab="shopping"',
            'data-tab="navigation"',
            'data-tab="promotions"',
            'data-tab="profile"',
            'data-tab="checkout"',
        ],
    )
    return body


def patch_weight_alert(body):
    body = re.sub(
        r'<div class="facts">.*?</div>\s*(?=<div class="steps">)',
        '<div class="facts" id="f-wa-facts"></div>\n  ',
        body,
        flags=re.S,
    )
    body = body.replace(
        '<button class="btn">重新感應商品</button>',
        '<button class="btn" onclick="APP.callApi(\'retry_weight\')">重新感應商品</button>',
    )
    body = body.replace(
        '<button class="btn btn-primary">放棄此次動作</button>',
        '<button class="btn btn-primary" onclick="APP.callApi(\'void_pending\')">放棄此次動作</button>',
    )
    body = body.replace(
        '<button class="btn btn-dashed">重新校準歸零</button>',
        '<button class="btn btn-dashed" onclick="APP.toast(\'重新校準歸零：尚未串接（規劃中）\')">重新校準歸零</button>',
    )
    body = body.replace(
        '<button class="btn btn-dashed">呼叫店員協助</button>',
        '<button class="btn btn-dashed" onclick="APP.toast(\'呼叫店員協助：尚未串接（規劃中）\')">呼叫店員協助</button>',
    )
    return body


def patch_checkout(body):
    body = re.sub(
        r'<div class="item-row">.*?</div>\s*<div class="item-row">.*?</div>',
        '<div id="f-co-items"></div>',
        body,
        flags=re.S,
        count=1,
    )
    body = re.sub(
        r'<div class="totals">.*?</div>\s*(?=</div>\s*<div class="section")',
        '<div class="totals" id="f-co-totals"></div>\n',
        body,
        flags=re.S,
    )
    body = body.replace(
        '<button class="back-btn">✕ 返回繼續選購</button>',
        '<button class="back-btn" onclick="APP.toast(\'目前狀態機還沒有「解除鎖定」事件，暫時無法從這裡返回——已知待補\')">✕ 返回繼續選購</button>',
    )
    body = body.replace(
        '<div class="pay-btn selected">LINE Pay</div>',
        '<div class="pay-btn selected" onclick="APP.selectPay(this)">LINE Pay</div>',
    )
    body = body.replace(
        '<div class="pay-btn">信用卡支付</div>',
        '<div class="pay-btn" onclick="APP.selectPay(this)">信用卡支付</div>',
    )
    body = body.replace(
        '<button class="btn-primary">確認付款，推車直接離場</button>',
        '<button class="btn-primary" onclick="APP.callApi(\'confirm_payment\')">確認付款，推車直接離場</button>',
    )
    body = patch_nth_open_tags(
        body,
        r'<div class="navitem( active)?( nav-badge)?">',
        [
            'data-tab="shopping"',
            'data-tab="navigation"',
            'data-tab="promotions"',
            'data-tab="profile"',
            'data-tab="checkout"',
        ],
    )
    return body


def patch_exit_confirm(body):
    body = re.sub(r'\s*<span class="badge-new">.*?</span>', "", body, flags=re.S)
    body = body.replace(
        "應付金額 $380 已成功扣款",
        '<span id="f-exit-amount">應付金額已成功扣款</span>',
    )
    body = body.replace('<div class="timer-count">02:57</div>', '<div class="timer-count" id="f-exit-timer">--:--</div>')
    body = body.replace(
        '<button class="btn-dashed">取消本次結帳／需要協助</button>',
        '<button class="btn-dashed" onclick="APP.toast(\'需要協助：尚未串接店員通知系統（規劃中）\')">取消本次結帳／需要協助</button>',
    )
    return body


PATCHERS = {
    "main": patch_main,
    "login": patch_login,
    "loading_profile": patch_loading_profile,
    "allergens": patch_allergens,
    "diet": patch_diet,
    "religion": patch_religion,
    "budget": patch_budget,
    "shopping": patch_shopping,
    "navigation": patch_navigation,
    "promotions": patch_promotions,
    "weight_alert": patch_weight_alert,
    "checkout": patch_checkout,
    "exit_confirm": patch_exit_confirm,
}


def main():
    all_css = []
    all_html = []
    for fname, sid in SCREENS:
        style, body = extract(fname)
        body = PATCHERS[sid](body)
        all_css.append(scope_css(style, sid))
        all_html.append(f'<div class="screen" id="screen-{sid}">\n{body}\n</div>')

    css = "\n".join(all_css)
    html_screens = "\n".join(all_html)

    page = TEMPLATE.replace("/*__SCREEN_CSS__*/", css).replace("<!--__SCREENS__-->", html_screens)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(page, encoding="utf-8")
    print(f"wrote {OUT_PATH} ({len(page)} bytes)")


TEMPLATE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>SmartCart</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Kalam:wght@400;700&display=swap');
  * { box-sizing: border-box; }
  html, body { margin:0; padding:0; background:#111; }
  body { width:720px; height:1280px; margin:0 auto; font-family:'Kalam','Comic Sans MS',cursive; position:relative; overflow:hidden; }
  .screen { display:none; position:absolute; inset:0; }
  .screen.active { display:flex; }

  /* 通用 checkbox/radio 視覺勾選（不依賴各畫面原本手畫的 SVG，統一用 CSS 畫） */
  .chip.selected .box::after, .chip-row.selected .box::after { content:"✓"; color:#fff; font-weight:700; font-size:14px; }
  .radio-row.selected .radio-dot::after { content:""; display:block; width:10px; height:10px; border-radius:50%; background:#fff; }
  .opt-selected { background:#1a1a1a !important; color:#fff !important; }
  .pay-btn.selected { background:#1a1a1a !important; color:#fff !important; }

  /* 全域工具列：退出 / 開發測試面板開關，疊在所有畫面最上層 */
  #app-toolbar { position:absolute; top:0; right:0; z-index:100000; display:flex; gap:6px; padding:6px; }
  #app-toolbar button {
    min-width:34px; height:34px; border-radius:17px; border:2px solid rgba(26,26,26,0.4);
    background:rgba(253,252,249,0.6); font-size:14px; font-family:sans-serif; line-height:1;
    padding:0 9px; font-weight:700;
  }
  #app-toolbar button:active { background:rgba(26,26,26,0.15); }

  #f-global-error {
    position:absolute; top:44px; left:12px; right:12px; z-index:99998;
    background:#fff3f3; border:2px solid #b00020; color:#b00020; border-radius:8px;
    padding:10px 14px; font-size:16px;
  }
  #f-global-error[hidden] { display:none; }

  #f-toast {
    position:absolute; left:50%; bottom:40px; transform:translateX(-50%); z-index:99999;
    background:#1a1a1a; color:#fff; padding:12px 20px; border-radius:20px; font-size:16px;
    max-width:80%; text-align:center; opacity:0; transition:opacity .2s;
  }
  #f-toast.show { opacity:1; }

  /* 開發測試面板：真實硬體（掃描器/UART/閘門）還沒做好之前，用這裡模擬事件 */
  #dev-panel {
    position:absolute; top:0; left:0; right:0; bottom:0; z-index:99997;
    background:rgba(20,20,20,0.94); color:#fff; padding:50px 20px 20px; overflow:auto;
    font-family:'Kalam','Comic Sans MS',cursive;
  }
  #dev-panel[hidden] { display:none; }
  #dev-panel h2 { font-size:20px; margin:18px 0 8px; border-bottom:1px dashed #666; padding-bottom:4px; }
  #dev-panel h2:first-of-type { margin-top:0; }
  #dev-panel .row { display:flex; gap:8px; margin-bottom:8px; align-items:center; flex-wrap:wrap; }
  #dev-panel input { flex:1; min-width:120px; border:2px solid #666; border-radius:6px; padding:8px; font:inherit; background:#222; color:#fff; }
  #dev-panel button { border:2px solid #888; border-radius:6px; padding:8px 12px; font:inherit; background:#333; color:#fff; }
  #dev-panel button:active { background:#555; }
  #dev-panel .state-dump { font-size:13px; color:#aaa; white-space:pre-wrap; word-break:break-all; margin-top:12px; }
  #dev-panel .note { font-size:14px; color:#f4c542; margin:-2px 0 10px; line-height:1.5; }

/*__SCREEN_CSS__*/

  /* ------------------------------------------------------------------
     這一段必須放在所有 #screen-* 規則「之後」，才蓋得過它們（id 選擇器優先
     度比 class 高，所以這裡要用 !important）。

     原因：每個畫面的 CSS 是從設計稿的 `body { width:720px; height:1280px }`
     換算來的，寫死 1280px。但實際視窗的可視高度常常小於 1280（pywebview 的
     視窗標題列/工作列會吃掉幾十 px），固定高度加上 overflow:hidden 的結果就
     是最底下的導覽列被裁掉看不到。改成填滿實際可視高度，flex 排版會自動把
     底部導覽列推到真正的畫面底部。全螢幕 720x1280 時兩者一樣，不影響。
     ------------------------------------------------------------------ */
  html, body { width:100%; height:100%; max-width:720px; }
  .screen { width:100% !important; height:100% !important; }
</style>
</head>
<body>

<div id="f-global-error" hidden></div>
<div id="f-toast"></div>
<div id="app-toolbar">
  <!-- 原本這兩顆用的是 emoji（🛠）。Pi OS 預設沒有裝彩色 emoji 字型，實機上
       會變成一個看不懂的豆腐方框，找不到開發面板在哪——改用純文字標籤。 -->
  <button title="開發測試面板" onclick="APP.toggleDevPanel()">DEV</button>
  <button title="退出" onclick="APP.callApi('quit')">✕</button>
</div>

<!--__SCREENS__-->

<div id="dev-panel" hidden>
  <!-- 唯讀診斷面板。刻意沒有任何「製造事件」的按鈕：這支程式只走真實硬體，
       如果這裡能按一下就生出一筆掃碼或秤重，那就等於又多了一條旁路，
       出問題時又要先搞清楚自己走在哪條路上。 -->
  <h2>硬體狀態（唯讀）</h2>
  <div class="note" id="dev-scan-status">最近收到的條碼：（尚未收到任何條碼）</div>
  <div class="note" id="dev-weight-status">秤重：（尚未收到讀數）</div>
  <div class="note" id="dev-gate-status">門口 BLE 判定次數：0</div>

  <h2>刷了條碼卻沒反應時，照這個順序看</h2>
  <div class="note">
    (1)「最近收到的條碼」有跟著變嗎？<br>
    &nbsp;&nbsp;&nbsp;沒變 → 條碼根本沒進到程式。掃描器沒接好，或 evdev 抓錯裝置。
    另開終端機跑 <code>sudo python3 -m drivers.barcode_scanner --list</code> 確認裝置名稱，
    對照 config.json 的 barcode_scanner.device_name_hint。<br>
    &nbsp;&nbsp;&nbsp;有變 → 條碼進來了，往下看。<br>
    (2) 畫面上方有紅色錯誤條嗎？那就是狀態機拒絕的原因，照著處理即可
    （最常見是「查無商品」＝這個條碼還沒建檔，用 tools/product_admin.py 建）。<br>
    (3)「秤重」那行有數字而且會跳動嗎？沒有的話 UART 斷了——掃碼會被狀態機
    擋下來，因為沒有重量就無法驗證掃了什麼。<br>
    (4) 還在「等待進入管制區」嗎？那時候掃碼本來就會被拒絕，要先推車通過門口
    讓 BLE 判定進場。
  </div>

  <h2>目前完整狀態</h2>
  <div class="state-dump" id="dev-state-dump"></div>
</div>

<script>
(function () {
  "use strict";

  var SHOPPING_FAMILY = ["shopping", "navigation", "promotions"];
  var NAV_TABS = ["shopping", "navigation", "promotions", "profile", "checkout"];
  var currentTab = "shopping";
  var lastState = null;
  var toastTimer = null;

  function esc(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function showScreen(id) {
    document.querySelectorAll(".screen").forEach(function (el) {
      el.classList.toggle("active", el.id === "screen-" + id);
    });
    document.querySelectorAll(".navitem[data-tab]").forEach(function (el) {
      el.classList.toggle("active", el.dataset.tab === id);
    });
  }

  function showTab(id) {
    if (id === "checkout") { callApi("lock_checkout"); return; }
    if (id === "profile") { toast("「我的」功能尚未實作"); return; }
    currentTab = id;
    showScreen(id);
  }

  function toast(msg) {
    var el = document.getElementById("f-toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove("show"); }, 2600);
  }

  function toggleDevPanel() {
    var el = document.getElementById("dev-panel");
    el.hidden = !el.hidden;
  }

  function callApi(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api[name]) {
      console.warn("pywebview api not ready:", name, args);
      return Promise.resolve(null);
    }
    return window.pywebview.api[name].apply(null, args).then(render).catch(function (err) {
      console.error(err);
      toast("發生錯誤：" + err);
    });
  }

  function wizardToggle(kind, name) {
    if (kind === "allergens") callApi("wizard_toggle_allergen", name);
    else if (kind === "religion") callApi("wizard_toggle_religion", name);
  }

  function wizardSetDiet(value) {
    callApi("wizard_set_diet", value);
  }

  var pendingBudgetValue = null;
  function budgetPick(value) {
    pendingBudgetValue = value;
    document.querySelectorAll("#screen-budget .opt").forEach(function (el) {
      el.classList.toggle("opt-selected", el.dataset.value === value);
    });
  }

  function confirmBudget() {
    var v = pendingBudgetValue;
    if (v == null) {
      var custom = document.getElementById("f-budget-custom");
      v = custom && custom.value ? custom.value : "unlimited";
    }
    callApi("set_budget", v);
  }

  function selectPay(el) {
    var parent = el.parentElement;
    Array.prototype.forEach.call(parent.children, function (c) { c.classList.remove("selected"); });
    el.classList.add("selected");
  }

  // ------------------------------------------------------------------
  function renderCartItems(containerId, items, withIndex) {
    var el = document.getElementById(containerId);
    if (!el) return;
    if (!items.length) {
      el.innerHTML = '<div style="color:#999;padding:8px 0;">（購物車目前是空的）</div>';
      return;
    }
    el.innerHTML = items.map(function (it, i) {
      var label = (withIndex ? (i + 1) + ". " : "") + esc(it.name) + (it.quantity > 1 ? " ×" + it.quantity : "");
      return '<div class="item-row"><span>' + label + '</span><span>$' + it.subtotal.toFixed(0) + '</span></div>';
    }).join("");
  }

  function render(payload) {
    if (!payload) return;
    lastState = payload;

    var errEl = document.getElementById("f-global-error");
    if (payload.last_error) { errEl.textContent = payload.last_error; errEl.hidden = false; }
    else { errEl.hidden = true; }
    var loginErrEl = document.getElementById("f-login-error");
    if (loginErrEl) loginErrEl.textContent = payload.screen === "login" ? (payload.last_error || "") : "";

    if (SHOPPING_FAMILY.indexOf(payload.screen) !== -1 || SHOPPING_FAMILY.indexOf(currentTab) !== -1 && payload.screen === "shopping") {
      if (SHOPPING_FAMILY.indexOf(currentTab) === -1) currentTab = "shopping";
      showScreen(currentTab);
    } else {
      currentTab = "shopping";
      showScreen(payload.screen);
    }

    // 載入會員資料
    var nameEl = document.getElementById("f-loading-member-name");
    if (nameEl) nameEl.textContent = payload.member_name || payload.member_id || "會員";

    // 偏好精靈：反映目前已選項目
    document.querySelectorAll("#screen-allergens .chip[data-name]").forEach(function (el) {
      el.classList.toggle("selected", payload.wizard_profile.allergens.indexOf(el.dataset.name) !== -1);
    });
    document.querySelectorAll("#screen-diet .radio-row[data-value]").forEach(function (el) {
      el.classList.toggle("selected", payload.wizard_profile.diet === el.dataset.value);
    });
    document.querySelectorAll("#screen-religion .chip-row[data-name]").forEach(function (el) {
      el.classList.toggle("selected", payload.wizard_profile.religion.indexOf(el.dataset.name) !== -1);
    });

    // 購物主畫面
    var header = document.getElementById("f-shop-header");
    if (header) {
      var budgetText = payload.budget != null ? ("$" + payload.budget.toFixed(0)) : "不限";
      header.textContent = (payload.member_name || payload.member_id || "訪客") +
        "　總額 = $" + payload.cart_total.toFixed(0) + " ／ 預算 = " + budgetText;
    }
    var fillEl = document.getElementById("f-progress-fill");
    if (fillEl) {
      var pct = payload.budget ? Math.min(100, (payload.cart_total / payload.budget) * 100) : 0;
      fillEl.style.width = pct.toFixed(0) + "%";
      var pctEl = document.getElementById("f-progress-pct");
      if (pctEl) pctEl.textContent = payload.budget ? (pct.toFixed(0) + "% 已使用") : "無預算上限";
    }
    var countEl = document.getElementById("f-cart-count-line");
    if (countEl) countEl.textContent = "購物車 = " + payload.cart_count + " 件";

    renderCartItems("f-cart-items", payload.cart_items, true);
    renderCartItems("f-co-items", payload.cart_items, true);

    var coTotals = document.getElementById("f-co-totals");
    if (coTotals) {
      coTotals.innerHTML = '<div class="final">應付金額 = $' + payload.cart_total.toFixed(0) + '</div>';
    }

    var pendingWrap = document.getElementById("f-pending-box-wrap");
    if (pendingWrap) {
      if (payload.awaiting_gate_entry) {
        // 精靈走完了，但 BLE 還沒判定推車通過門口。這時候狀態機還在
        // 「已登入、未進管制區」，掃碼會被拒絕——所以要明講在等什麼，
        // 不要讓畫面看起來已經可以開始購物了。
        pendingWrap.innerHTML =
          '<div class="pending-box"><div class="mode-badge">等待進入管制區</div>' +
          '<div class="pending-line">請推車通過管制區入口，系統會自動偵測進場</div>' +
          '<div class="pulse-row"><div class="pulse-dot"></div>偵測中 ... 進場前掃碼不會被受理</div></div>';
      } else if (payload.pending_item_barcode) {
        var modeText = payload.pending_item_mode === "remove" ? "移除購物車" : "加入購物車";
        pendingWrap.innerHTML =
          '<div class="pending-box">' +
          '<div class="mode-badge">系統判定：' + modeText + '</div>' +
          '<div class="pending-line">已掃描條碼：' + esc(payload.pending_item_barcode) + '</div>' +
          '<div class="pulse-row"><div class="pulse-dot"></div>核對中 ...　目前重量 ' +
          (payload.current_weight_g != null ? payload.current_weight_g.toFixed(1) + 'g' : '（無讀數）') + '</div>' +
          '</div>';
      } else if (payload.unscanned_baseline_g != null) {
        pendingWrap.innerHTML =
          '<div class="pending-box"><div class="mode-badge">偵測到重量變化，尚未掃碼</div>' +
          '<div class="pending-line">變化前基準值：' + payload.unscanned_baseline_g.toFixed(1) + 'g　目前：' +
          (payload.current_weight_g != null ? payload.current_weight_g.toFixed(1) + 'g' : '（無讀數）') + '</div>' +
          '<div class="pulse-row"><div class="pulse-dot"></div>請掃描剛才拿取／放入的商品</div></div>';
      } else {
        pendingWrap.innerHTML = "";
      }
    }

    // 重量異常
    var waFacts = document.getElementById("f-wa-facts");
    if (waFacts) {
      var lines = [];
      if (payload.pending_item_barcode) {
        lines.push("比對中商品：條碼 " + esc(payload.pending_item_barcode) + "　自動判定方向：" +
          (payload.pending_item_mode === "remove" ? "移除" : "加入"));
      } else {
        lines.push("這筆是「拿取／放入後忘記掃碼」逾時觸發的異常，沒有對應的條碼可核對");
      }
      lines.push("目前秤重讀數 = " + (payload.current_weight_g != null ? payload.current_weight_g.toFixed(1) + "g" : "（無讀數）"));
      lines.push("感測器狀態 = " + (payload.sensor_connected ? "正常" : "已斷線"));
      waFacts.innerHTML = lines.map(function (l) { return "<div>" + l + "</div>"; }).join("");
    }

    var exitAmount = document.getElementById("f-exit-amount");
    if (exitAmount) exitAmount.textContent = "應付金額 $" + payload.cart_total.toFixed(0) + " 已成功扣款";
    var exitTimer = document.getElementById("f-exit-timer");
    if (exitTimer) exitTimer.textContent = payload.exit_countdown || "--:--";

    // 感測器狀態燈號：這一格原本是設計稿裡寫死的「✓ 感測器正常」，不管實際
    // 上有沒有秤重資料都長一樣——而「完全沒有秤重讀數」正是掃碼會被狀態機
    // 拒絕的主因（見 CartStateMachine._on_item_scanned 開頭的檢查），畫面卻
    // 顯示一切正常，等於在誤導人。改成反映真實狀態。
    var sensorText = document.getElementById("f-sensor-text");
    if (sensorText) {
      if (!payload.sensor_connected) sensorText.textContent = "感測器斷線";
      else if (!payload.has_weight_reading) sensorText.textContent = "無秤重資料";
      else sensorText.textContent = "感測器正常";
    }

    // 唯讀診斷面板
    var scanStatus = document.getElementById("dev-scan-status");
    if (scanStatus) {
      scanStatus.textContent = "最近收到的條碼：" + (payload.last_scan || "（尚未收到任何條碼）");
    }
    var weightStatus = document.getElementById("dev-weight-status");
    if (weightStatus) {
      weightStatus.textContent =
        "秤重：" + (payload.current_weight_g != null ? payload.current_weight_g.toFixed(1) + " g" : "（尚未收到讀數）") +
        "　感測器連線 = " + (payload.sensor_connected ? "正常" : "斷線");
    }
    var gateStatus = document.getElementById("dev-gate-status");
    if (gateStatus) {
      gateStatus.textContent = "門口 BLE 判定次數：" + payload.gate_events +
        (payload.awaiting_gate_entry ? "　（目前狀態：等待進入管制區）" : "");
    }

    var dump = document.getElementById("dev-state-dump");
    if (dump) dump.textContent = JSON.stringify(payload, null, 1);
  }

  document.body.addEventListener("click", function (e) {
    var nav = e.target.closest(".navitem[data-tab]");
    if (nav) showTab(nav.dataset.tab);
  });

  // 條碼由 evdev 直接從 /dev/input/event* 讀取（drivers/barcode_scanner.py），
  // 完全不經過前端——掃描器的按鍵事件被 grab() 獨佔，這個頁面收不到、也不需要
  // 收到。所以這裡沒有任何「把按鍵拼成條碼」的邏輯，只留下 Esc 退出。
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") callApi("quit");
  });

  function boot() {
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.get_state().then(render);
    } else {
      setTimeout(boot, 200);
    }
  }

  window.addEventListener("pywebviewready", boot);
  setTimeout(boot, 500); // 保險：萬一 pywebviewready 事件沒發生也能啟動

  window.APP = {
    callApi: callApi, showTab: showTab, toast: toast, toggleDevPanel: toggleDevPanel,
    wizardToggle: wizardToggle, wizardSetDiet: wizardSetDiet,
    budgetPick: budgetPick, confirmBudget: confirmBudget, selectPay: selectPay,
    render: render,
  };
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
