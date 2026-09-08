"""
tools/preview_ui.py

在真的 7 吋觸控螢幕（720×1280）上直接預覽 UI 線框稿，不透過 Claude Design
畫布的線上預覽——那邊因為是把所有畫面縮放塞進同一個畫布來顯示，各頁內容
高度不一致時，縮放比例/裁切位置會跟著不一樣，看起來像「底部導覽列每頁位
置不一樣」，但這其實是畫布預覽本身的呈現方式問題，不是每份 HTML 檔案本身
的排版問題。這支工具繞過那層畫布，把設計稿轉成一般網頁後，直接開一個跟螢
幕等大的視窗（不縮放、不裁切、1:1 像素），這樣才能看到文字大小、容器 flex
排版、觸控熱區在「實際硬體」上到底好不好用。

畫面來源：`tools/ui_preview/pages/*.html`，是從 Claude Design 畫布用的
`design-cart-ui/*.dc.html`（依賴畫布本身的 `support.js` 才能正確渲染）轉
出來的標準 HTML，可以直接被任何瀏覽器/webview 開啟，不需要額外套件。如果
之後線框稿又改了，要重新轉換一次（轉換腳本沒有隨專案交付，是一次性用的；
如果之後常態性需要重轉，之後可以再補一支固定收在 tools/ 底下）。

用法（需要先 `pip install pywebview`，Pi 上通常還需要
`sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1`
讓 pywebview 找得到 GTK/WebKit 後端——找不到 4.1 這個套件名稱的話改裝
gir1.2-webkit2-4.0。這些套件本來就沒放進 requirements.txt，因為這支只是
臨時的螢幕預覽工具，不是正式功能，避免把非必要依賴混進正式清單）：

    python3 -m tools.preview_ui                  # 從第一頁開始，視窗鎖 720x1280
    python3 -m tools.preview_ui --start Shopping  # 從指定頁面開始（檔名不用打 .html）
    python3 -m tools.preview_ui --fullscreen      # 全螢幕（螢幕剛好是 720x1280 時建議加這個）
    python3 -m tools.preview_ui --list            # 列出所有可預覽的頁面代號，不開視窗

畫面上兩側會有小小的半透明圓鈕（‹ ›）可以點著切換上一頁/下一頁，觸控直接
點就可以，不需要鍵盤；有接鍵盤的話左右方向鍵、空白鍵也可以翻頁，`Esc` 鍵
直接結束整支程式（不只是退出全螢幕）。這組導覽鈕是這支工具自己疊加上去
的，不是正式 UI 的一部分，正式畫面裡最下面那條「常駐底部導覽列」是每份頁
面自己的內容，這支工具完全沒有動它，所以在這裡看到的底部導覽列位置就是
它在真實裝置上會呈現的位置。

**退出方式**：畫面上按 `Esc` 鍵（有接鍵盤的話）；沒有鍵盤/卡住沒反應的
話，從另一個終端機（例如另開一個 SSH 連線）執行 `pkill -f tools.preview_ui`
直接砍掉行程，不用管畫面上是什麼狀態。

**如果畫面花掉／出現條紋亂碼**：這是 WebKitGTK 硬體合成（GPU compositing）
在某些顯示環境（尤其透過 VNC/遠端螢幕分享這類會重新截取畫面合成結果的情
境）下的已知相容性問題，不是這份 HTML 本身壞掉。這支工具已經預設關閉硬體
合成來避開這個問題（見下面 `WEBKIT_DISABLE_COMPOSITING_MODE`）；如果還是
花屏，先拿掉 `--fullscreen` 用一般視窗模式測，排除全螢幕合成路徑的問題，
並盡量在「接實體螢幕」而不是透過遠端畫面分享看的狀態下確認。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path
from typing import List

# WebKitGTK 在部分顯示環境（尤其遠端 VNC/螢幕分享這類會重新截取畫面合成結
# 果的情境）下，硬體合成（GPU compositing）路徑容易出現花屏/條紋亂碼，這是
# pywebview GTK 後端的已知問題，跟這份 HTML 本身無關。要在 import webview
# 之前設，晚了沒用。
os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

_PAGES_DIR = Path(__file__).resolve().parent / "ui_preview" / "pages"
_MANIFEST_PATH = Path(__file__).resolve().parent / "ui_preview" / "manifest.json"

SCREEN_W = 720
SCREEN_H = 1280


def load_manifest() -> List[dict]:
    if not _MANIFEST_PATH.exists():
        raise SystemExit(
            f"[錯誤] 找不到 {_MANIFEST_PATH}，確認 tools/ui_preview/ 資料夾有跟這支工具一起送過來"
        )
    with open(_MANIFEST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class NavAPI:
    """給前端小圓鈕呼叫的 Python 端 API（pywebview 的 js_api 機制）：
    前端按鈕點擊 -> window.pywebview.api.go(delta) -> 這裡算出下一頁路徑 ->
    直接把整個視窗導到下一頁的檔案（等於重新整個載入一份全新頁面，跟真實使
    用情境下「切換畫面＝載入新頁面」比較接近，不是用 JS 局部替換內容）。
    """

    def __init__(self, pages: List[dict], start_index: int, window_holder: dict):
        self.pages = pages
        self.index = start_index
        self._window_holder = window_holder

    def go(self, delta: int) -> None:
        self.index = (self.index + delta) % len(self.pages)
        window = self._window_holder.get("window")
        if window is not None:
            page = self.pages[self.index]
            window.load_url((_PAGES_DIR / page["file"]).as_uri())

    def quit(self) -> None:
        window = self._window_holder.get("window")
        if window is not None:
            window.destroy()


def _resolve_start_index(pages: List[dict], start: str | None) -> int:
    if not start:
        return 0
    target = start.strip()
    for i, p in enumerate(pages):
        stem = p["file"].rsplit(".", 1)[0]
        if stem.lower() == target.lower():
            return i
    names = ", ".join(p["file"].rsplit(".", 1)[0] for p in pages)
    raise SystemExit(f"[錯誤] 找不到頁面 '{start}'，可用的名稱：{names}")


def _main() -> int:
    parser = argparse.ArgumentParser(description="在真實螢幕上（1:1 像素）預覽 UI 線框稿")
    parser.add_argument("--start", default=None, help="起始頁面檔名（不用打 .html），例如 Shopping")
    parser.add_argument("--fullscreen", action="store_true", help="全螢幕開啟（螢幕本身就是 720x1280 時建議加這個）")
    parser.add_argument("--list", action="store_true", help="列出所有可預覽的頁面代號，不開視窗")
    args = parser.parse_args()

    pages = load_manifest()

    if args.list:
        print("可用頁面（依建議測試順序）：")
        for i, p in enumerate(pages, start=1):
            stem = p["file"].rsplit(".", 1)[0]
            print(f"  {i:>2}  {stem:<16} {p['title']}")
        return 0

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

    start_index = _resolve_start_index(pages, args.start)
    window_holder: dict = {"window": None}
    api = NavAPI(pages, start_index, window_holder)

    start_url = (_PAGES_DIR / pages[start_index]["file"]).as_uri()
    window = webview.create_window(
        "SmartCart UI 預覽",
        start_url,
        width=SCREEN_W,
        height=SCREEN_H,
        resizable=False,
        frameless=args.fullscreen,
        fullscreen=args.fullscreen,
        js_api=api,
    )
    window_holder["window"] = window

    def on_loaded():
        # 有實體鍵盤時的方便鍵：左右鍵/空白鍵翻頁，Esc 直接結束整支程式
        # （不是只退出全螢幕）——沒有鍵盤/畫面卡住的話，用另一個終端機
        # `pkill -f tools.preview_ui` 一樣能結束。
        window.evaluate_js(
            """
            document.addEventListener('keydown', function (e) {
                if (e.key === 'ArrowRight' || e.key === ' ') {
                    window.pywebview.api.go(1);
                } else if (e.key === 'ArrowLeft') {
                    window.pywebview.api.go(-1);
                } else if (e.key === 'Escape') {
                    window.pywebview.api.quit();
                }
            });
            """
        )

    window.events.loaded += on_loaded

    # Ctrl+C 在終端機也要能乾淨結束（預設 SIGINT 在某些 GTK 主迴圈下會被吃掉）。
    signal.signal(signal.SIGINT, lambda *_: window.destroy())

    print(f"共 {len(pages)} 頁，從「{pages[start_index]['title']}」開始，畫面上點兩側圓鈕切換。")
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
