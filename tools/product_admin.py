"""
tools/product_admin.py

商品資料庫的後台管理工具（純 CLI）。

背景：之前實測「刷真的商品條碼，畫面完全沒反應」，追查後有兩個原因疊在一
起——(1) `ui/app_gui.py` 對「狀態機拒絕了這次掃碼」的情況沒有顯示錯誤訊
息（這個已經修掉了，見該檔案的更新說明），(2) 更根本的：`database/db_manager.py`
出廠只 seed 了 5 筆寫死的測試商品，刷任何一個沒建檔的真實商品條碼，狀態機
一定會判定「查無此商品」而拒絕——不是程式邏輯壞掉，是資料庫裡根本沒有這個
商品。這支工具就是用來補這一塊：建立、修改、刪除、匯入你實際會用到的商品
資料，不用寫程式、也不用直接碰 SQL。

跟現有 `database/db_manager.py --init`/`--list` 的分工：那支負責建表跟灌測
試資料，是給開發流程用的；這支才是「日常維護商品資料」的後台，商品本身的
新增/修改/刪除/批次匯入都在這裡做，兩支工具共用同一個 `DBManager`，不會有
兩套資料庫邏輯。

條碼欄位怎麼輸入：USB 條碼掃描器本身就是模擬鍵盤（HID keyboard）輸入，刷
一下就是把條碼字串 + Enter 直接打進目前有游標焦點的欄位——跟你自己用鍵盤打
字、按下 Enter 完全一樣。所以條碼欄位就是一般的文字輸入（`input()`），把游
標停在那個提示上直接刷就會自動填進去、自動送出，不需要另外寫一套背景程式
去監聽掃描器的硬體事件。之前這支工具還有一個 `scan` 指令，是用
`drivers.barcode_scanner`（透過 evdev 直接讀底層鍵盤事件、還會把裝置
「獨佔」掉）做的，跟這支互動式 CLI 的用法疊在一起反而會卡住（獨佔掉輸入裝
置之後，終端機本身也收不到那個裝置的按鍵事件了）——這是多餘的複雜度，已經
拿掉，統一都走 `add`/`edit` 這組手動輸入邏輯，條碼欄位直接刷即可。

標準重量欄位怎麼輸入：接了真的 HX711 秤重感測器時（透過 UART，跟購物車正
式運作時同一顆感測器、同一份 `config.json` 校正值），`add`/`edit` 的重量步
驟會提示你把商品放上秤重感應區、按 Enter 現場量測，量到的公克數直接當作
`standard_weight_g`，不用自己拿磅秤讀數再手動打字進去——這樣量到的數字才會
跟購物車實際秤重比對時用的是同一套校正基準，不會有人工謄寫打錯或校正基準
兜不起來的問題。容許誤差（`weight_tolerance_g`）預設自動抓量到重量的
10%，一樣不用手動輸入（要自訂的話可以直接覆蓋，見下方欄位說明）。

**重量沒有手動輸入的退路**：秤連不上時 `add`/`edit` 會直接報錯結束。手打的
數字跟這台秤在這份校正值下量出來的數字之間差多少沒人知道，用手打的值建檔，
購物時的秤重比對就注定會偏、而且偏在哪裡完全看不出來。其他不需要重量的指令
（list/delete/import/export/member-*）沒接秤照樣能用。

用法：
    python3 -m tools.product_admin                        # 互動選單（不熟指令的話用這個）
    python3 -m tools.product_admin list                    # 列出所有商品
    python3 -m tools.product_admin add                     # 互動輸入新增一筆（條碼可直接用掃描器刷）
    python3 -m tools.product_admin add --barcode 471... --name "..." --price 39
        # 命令列參數模式：條碼/名稱/單價用參數給，重量一樣要現場量（沒有 --weight）
    python3 -m tools.product_admin edit 4710018001234       # 互動修改一筆（Enter 保留原值）
    python3 -m tools.product_admin delete 4710018001234
    python3 -m tools.product_admin import products.csv       # 從 CSV 批次匯入/更新
    python3 -m tools.product_admin export products_backup.csv

    全部指令都可以加 --db /path/to/inventory.db 指定資料庫路徑（預設跟
    database/db_manager.py 一樣，是 database/inventory.db）；`add`/`edit`
    另外可以加 --port/--baud 指定秤重感測器的 UART 連接埠（預設讀
    config.json 的 serial 設定，跟購物車正式運作時同一份）。

CSV 格式（匯入/匯出都用這個欄位順序，第一行是標題列）：
    barcode,name,unit_price,standard_weight_g,weight_tolerance_g

    - barcode：商品條碼（文字，保留前導 0）
    - unit_price：單價（元）
    - standard_weight_g：這個商品的標準重量（公克）——秤重比對就是拿實際
      量到的重量變化去跟這個值比對，量錯的話「明明拿對商品」也會被判定成
      秤重異常，所以這欄要盡量量準（互動輸入時建議直接放上秤現場量，不要
      手動打字猜）
    - weight_tolerance_g：容許誤差（公克）——同一項商品不同顆/包裝本身的
      重量落差、秤本身的雜訊，都要算在這裡面，太小的話正常操作也會一直誤
      報異常，太大的話會抓不到真的偷竊/放錯商品。CSV 匯入沒有「自動 10%」
      這件事——這欄位是必填的，要自動抓 10% 的話用互動輸入（`add`/`edit`）
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db_manager import DBManager, Member, Product  # noqa: E402

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
_CSV_FIELDS = ["barcode", "name", "unit_price", "standard_weight_g", "weight_tolerance_g"]
_AUTO_TOLERANCE_RATIO = 0.10  # 自動容差 = 量到的標準重量 × 這個比例


class WeightGaugeUnavailable(RuntimeError):
    """秤重感測器連不上／量不到讀數。`add`/`edit` 會直接讓這個例外浮上來，
    不會退回手動輸入公克數（理由見 WeightGauge 的說明）。"""


# ----------------------------------------------------------------------
# 純邏輯部分（不碰輸入輸出，方便之後如果要補單元測試）
# ----------------------------------------------------------------------
def parse_product_row(row: dict) -> Product:
    """把一行 dict（不管是來自 CSV 還是互動輸入湊出來的）轉成 Product，數值
    欄位轉型失敗或缺欄位時丟 ValueError，訊息裡帶著是哪一筆、哪個欄位有問題
    （匯入大檔案時，只知道『第幾行壞掉』但不知道『壞在哪個欄位』很難排查）。
    """
    barcode = str(row.get("barcode", "")).strip()
    name = str(row.get("name", "")).strip()
    if not barcode:
        raise ValueError("barcode 不能是空的")
    if not name:
        raise ValueError(f"條碼 {barcode}：name 不能是空的")
    try:
        unit_price = float(row["unit_price"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"條碼 {barcode}：unit_price 看不懂（{row.get('unit_price')!r}），要填數字")
    try:
        standard_weight_g = float(row["standard_weight_g"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"條碼 {barcode}：standard_weight_g 看不懂（{row.get('standard_weight_g')!r}），要填數字")
    try:
        weight_tolerance_g = float(row["weight_tolerance_g"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"條碼 {barcode}：weight_tolerance_g 看不懂（{row.get('weight_tolerance_g')!r}），要填數字")
    if unit_price < 0:
        raise ValueError(f"條碼 {barcode}：unit_price 不能是負數")
    if standard_weight_g <= 0:
        raise ValueError(f"條碼 {barcode}：standard_weight_g 必須大於 0（這是拿來做秤重比對的基準）")
    if weight_tolerance_g < 0:
        raise ValueError(f"條碼 {barcode}：weight_tolerance_g 不能是負數")
    return Product(
        barcode=barcode,
        name=name,
        unit_price=unit_price,
        standard_weight_g=standard_weight_g,
        weight_tolerance_g=weight_tolerance_g,
    )


def import_csv(db: DBManager, csv_path: str) -> tuple[int, list[str]]:
    """回傳 (成功筆數, 失敗訊息列表)——就算某幾行有問題，其他正常的行還是
    會照樣匯入，不會因為第 30 行打錯字就讓前面 29 行全部匯入失敗。
    """
    ok = 0
    errors: list[str] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in _CSV_FIELDS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV 標題列缺少欄位：{missing}，需要的欄位是 {_CSV_FIELDS}")
        for i, row in enumerate(reader, start=2):  # 第 1 行是標題，資料從第 2 行開始
            try:
                product = parse_product_row(row)
            except ValueError as exc:
                errors.append(f"第 {i} 行：{exc}")
                continue
            db.upsert_product(product)
            ok += 1
    return ok, errors


def export_csv(db: DBManager, csv_path: str) -> int:
    products = db.list_products()
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for p in products:
            writer.writerow(p.to_dict())
    return len(products)


# ----------------------------------------------------------------------
# 秤重感測器：讓「標準重量」用現場量的，不是手動打字猜
# ----------------------------------------------------------------------
class WeightGauge:
    """包一層 `drivers.uart_receiver.UartReceiver`，讓互動輸入流程可以「把
    商品放上秤重感應區、按 Enter 量現在的重量」。換算公式（`raw_to_grams`）
    跟購物車正式運作時秤重比對用的是同一份，校正值（config.json 的
    `weight.hx711_offset`/`hx711_scale`）改了兩邊會一起生效，不會有「這支
    工具量出來的數字」跟「購物車實際判定用的數字」兜不起來的問題。

    連線是懶惰的（第一次真的呼叫 `measure()` 才會去嘗試連 UART），這樣執行
    `list`/`delete`/`import`/`export`/`member-*` 這種不需要秤重的指令時，
    不會平白多等好幾秒的連線逾時，也不會因為沒接秤就用不了。

    但 `add`/`edit` 需要量重量時連不上，就直接拋 `WeightGaugeUnavailable`
    ——**沒有手動輸入公克數的退路**。重量是秤重比對的基準，手打的數字跟秤
    出來的數字之間差多少沒人知道，用手打的值建檔，之後購物時的比對就注定
    會偏；寧可當下講清楚秤沒接，也不要讓一筆不可信的重量混進商品資料庫。
    """

    def __init__(self, port: str, baudrate: int, offset: float, scale: float,
                 num_samples: int = 20, sample_timeout_sec: float = 3.0,
                 connect_timeout_sec: float = 3.0):
        self.port = port
        self.baudrate = baudrate
        self.offset = offset
        self.scale = scale
        self.num_samples = num_samples
        self.sample_timeout_sec = sample_timeout_sec
        self.connect_timeout_sec = connect_timeout_sec
        self._receiver = None
        self._tried = False
        self._error: Optional[str] = None

    def _ensure_connected(self) -> None:
        if self._tried:
            return
        self._tried = True
        try:
            from drivers.uart_receiver import UartReceiver
            receiver = UartReceiver(port=self.port, baudrate=self.baudrate)
            receiver.start()
            deadline = time.time() + self.connect_timeout_sec
            got_packet = False
            while time.time() < deadline:
                if not receiver.out_queue.empty():
                    got_packet = True
                    break
                time.sleep(0.1)
            if not got_packet:
                receiver.stop()
                self._error = (
                    f"{self.connect_timeout_sec:.0f} 秒內沒有從 {self.port} 收到任何合法封包"
                    "（序列埠打不開、下位機沒在送、或封包格式不是 v2；上面幾行 ERROR 會講實際原因）"
                )
                return
            self._receiver = receiver
        except Exception as exc:  # noqa: BLE001 — 錯誤訊息留到真的要量重量時才丟出來
            self._error = str(exc)

    def require(self) -> None:
        """確認秤真的可用，不可用就拋 WeightGaugeUnavailable。"""
        self._ensure_connected()
        if self._receiver is None:
            raise WeightGaugeUnavailable(
                f"秤重感測器連不上：{self._error or '未知原因'}\n"
                f"  1) 確認下位機有在送 UART 封包，且 {self.port} 存在、目前使用者有權限"
                "（通常要在 dialout 群組）\n"
                "  2) 單獨測一次：python3 -m drivers.uart_receiver --port " + str(self.port) + "\n"
                "  3) 換一個序列埠：--port /dev/ttyXXX"
            )

    def measure(self) -> float:
        """量一次重量，回傳公克數。量不到就拋 WeightGaugeUnavailable。"""
        self.require()
        from core.weight_convert import raw_to_grams
        from tools.calibrate_weight import collect_hx711_samples

        # 清掉連線期間累積的舊封包，避免上一次量測（或上一個商品）殘留的
        # 樣本混進這一次的平均值。
        while True:
            try:
                self._receiver.out_queue.get_nowait()
            except Exception:
                break
        samples: List[int] = collect_hx711_samples(self._receiver.out_queue, self.num_samples, self.sample_timeout_sec)
        if not samples:
            raise WeightGaugeUnavailable(
                f"{self.sample_timeout_sec:.0f} 秒內沒有收到足夠的秤重樣本，UART 可能中途斷了"
            )
        raw_mean = sum(samples) / len(samples)
        return raw_to_grams(raw_mean, self.offset, self.scale)

    def close(self) -> None:
        if self._receiver is not None:
            self._receiver.stop()
            self._receiver = None


# ----------------------------------------------------------------------
# 互動輸入小工具
# ----------------------------------------------------------------------
def _prompt(label: str, default: Optional[str] = None) -> str:
    suffix = f"（目前：{default}，直接按 Enter 保留）" if default is not None else ""
    raw = input(f"{label}{suffix}：").strip()
    if not raw and default is not None:
        return default
    return raw


def _prompt_weight_and_tolerance(gauge: WeightGauge, existing: Optional[Product]) -> Optional[tuple[float, float]]:
    """回傳 (標準重量, 容許誤差)；輸入 q 取消回傳 None。

    重量**只能**從秤現場量。沒有手動輸入公克數這個選項——手打的數字跟這台秤
    在這份校正值下量出來的數字之間差多少沒人知道，用手打的值建檔，購物時的
    秤重比對就注定會偏掉，而且偏在哪裡完全看不出來。秤連不上就直接報錯。
    """
    gauge.require()  # 連不上就在這裡拋出來，不要問完一堆欄位才發現量不到

    default_w = existing.standard_weight_g if existing else None
    weight: Optional[float] = None
    while weight is None:
        hint = f"（Enter 量測，k 保留原值 {default_w}g，q 取消）" if default_w is not None else "（Enter 量測，q 取消）"
        raw = input(f"把商品放上秤重感應區，穩定後按 Enter 量測重量{hint}：").strip().lower()
        if raw == "q":
            return None
        if raw == "k" and default_w is not None:
            weight = default_w
        else:
            print("量測中...")
            weight = gauge.measure()
            print(f"量到的重量 = {weight:.1f} g")
        if weight is not None and weight <= 0:
            print("[錯誤] 量到的重量不大於 0。確認商品真的放在感應區上、秤有歸零過"
                  "（tools/calibrate_weight.py），然後重試。")
            weight = None

    auto_tolerance = round(weight * _AUTO_TOLERANCE_RATIO, 1)
    tol_raw = input(
        f"容許誤差（公克，直接按 Enter 用自動值 = 重量的 {_AUTO_TOLERANCE_RATIO*100:.0f}% "
        f"= {auto_tolerance}g；輸入 q 取消）："
    ).strip()
    if tol_raw.lower() == "q":
        return None
    if not tol_raw:
        return weight, auto_tolerance
    try:
        tolerance = float(tol_raw)
    except ValueError:
        print("[錯誤] 不是有效數字，改用自動值")
        return weight, auto_tolerance
    if tolerance < 0:
        print("[錯誤] 容許誤差不能是負數，改用自動值")
        return weight, auto_tolerance
    return weight, tolerance


def _prompt_product(gauge: WeightGauge, existing: Optional[Product] = None) -> Optional[Product]:
    """互動輸入一筆商品資料；existing 有給的話是「編輯模式」，直接 Enter 就
    保留原值。任何一步輸入 'q' 直接放棄這筆，回傳 None（避免使用者手滑輸入
    到一半想取消，卻只能硬著頭皮亂填完）。條碼欄位是普通的文字輸入——用實
    體掃描器刷，條碼字串會跟手動打字一樣直接進來，不需要另外處理。
    """
    print("（任何欄位輸入 q 可以取消這筆）")
    barcode = existing.barcode if existing else _prompt("條碼（可直接用掃描器刷）")
    if barcode.lower() == "q":
        return None
    name = _prompt("商品名稱", existing.name if existing else None)
    if name.lower() == "q":
        return None
    price_s = _prompt("單價（元）", str(existing.unit_price) if existing else None)
    if price_s.lower() == "q":
        return None

    weight_result = _prompt_weight_and_tolerance(gauge, existing)
    if weight_result is None:
        return None
    weight, tolerance = weight_result

    try:
        return parse_product_row({
            "barcode": barcode, "name": name, "unit_price": price_s,
            "standard_weight_g": weight, "weight_tolerance_g": tolerance,
        })
    except ValueError as exc:
        print(f"[錯誤] {exc}")
        return None


def _print_products(products: list[Product]) -> None:
    if not products:
        print("（目前資料庫裡沒有商品）")
        return
    print(f"{'條碼':<16} {'名稱':<24} {'單價':>8} {'標準重量(g)':>12} {'容差(±g)':>10}")
    print("-" * 76)
    for p in products:
        print(f"{p.barcode:<16} {p.name:<24} {p.unit_price:>8.0f} {p.standard_weight_g:>12.1f} {p.weight_tolerance_g:>10.1f}")
    print(f"共 {len(products)} 筆")


# ----------------------------------------------------------------------
# 各指令
# ----------------------------------------------------------------------
def cmd_list(db: DBManager, _args, _gauge: Optional[WeightGauge] = None) -> None:
    _print_products(db.list_products())


def cmd_add(db: DBManager, args, gauge: WeightGauge) -> None:
    if args.barcode:
        # 命令列參數模式：條碼/名稱/單價可以先用參數給完，但**重量還是要現場
        # 量**（沒有 --weight 這個參數）。容差一律自動抓量到重量的 10%。
        missing = [n for n in ("name", "price") if getattr(args, n) is None]
        if missing:
            print(f"[錯誤] --barcode 有給的話，{missing} 也都要一起給（或乾脆不加 --barcode 走互動輸入）")
            return
        result = _prompt_weight_and_tolerance(gauge, None)
        if result is None:
            print("已取消，沒有新增。")
            return
        weight, tolerance = result
        try:
            product = parse_product_row({
                "barcode": args.barcode, "name": args.name, "unit_price": args.price,
                "standard_weight_g": weight, "weight_tolerance_g": tolerance,
            })
        except ValueError as exc:
            print(f"[錯誤] {exc}")
            return
    else:
        product = _prompt_product(gauge)
        if product is None:
            print("已取消，沒有新增。")
            return

    existed = db.get_product(product.barcode) is not None
    db.upsert_product(product)
    print(f"{'已更新' if existed else '已新增'}：{product.barcode}　{product.name}　${product.unit_price:.0f}　{product.standard_weight_g:.1f}±{product.weight_tolerance_g:.1f}g")


def cmd_edit(db: DBManager, args, gauge: WeightGauge) -> None:
    existing = db.get_product(args.barcode)
    if existing is None:
        print(f"[錯誤] 條碼 {args.barcode} 不在資料庫裡，用 `add` 新增，不是 `edit`")
        return
    product = _prompt_product(gauge, existing=existing)
    if product is None:
        print("已取消，沒有異動。")
        return
    db.upsert_product(product)
    print(f"已更新：{product.barcode}　{product.name}　${product.unit_price:.0f}　{product.standard_weight_g:.1f}±{product.weight_tolerance_g:.1f}g")


def cmd_delete(db: DBManager, args, _gauge: Optional[WeightGauge] = None) -> None:
    existing = db.get_product(args.barcode)
    if existing is None:
        print(f"[錯誤] 條碼 {args.barcode} 不在資料庫裡，沒有東西可以刪")
        return
    if not args.yes:
        confirm = input(f"確定要刪除「{existing.name}」（條碼 {existing.barcode}）？輸入 y 確認：").strip().lower()
        if confirm != "y":
            print("已取消。")
            return
    db.delete_product(args.barcode)
    print(f"已刪除：{args.barcode}　{existing.name}")


def cmd_import(db: DBManager, args, _gauge: Optional[WeightGauge] = None) -> None:
    try:
        ok, errors = import_csv(db, args.csv_path)
    except (ValueError, FileNotFoundError) as exc:
        print(f"[錯誤] {exc}")
        return
    print(f"匯入完成：成功 {ok} 筆，失敗 {len(errors)} 筆")
    for e in errors:
        print(f"  - {e}")


def cmd_export(db: DBManager, args, _gauge: Optional[WeightGauge] = None) -> None:
    n = export_csv(db, args.csv_path)
    print(f"已匯出 {n} 筆商品到 {args.csv_path}")


# ----------------------------------------------------------------------
# 會員
# ----------------------------------------------------------------------
# `DBManager` 早就寫好 get_member/list_members/upsert_member/delete_member，
# 但兩支 CLI（本檔跟 database/db_manager.py）以前都沒有任何指令呼叫得到它們
# ——結果資料庫裡能存在的會員只有 db_manager.py 裡寫死的兩筆測試會員，要登錄
# 一張真的會員卡只能改原始碼重建資料庫，或自己開 sqlite3 下 SQL。這一組指令
# 就是補這個洞。
def cmd_member_list(db: DBManager, _args, _gauge: Optional[WeightGauge] = None) -> None:
    members = db.list_members()
    if not members:
        print("（資料庫裡目前沒有任何會員）")
        return
    print(f"{'會員代碼':<20} 姓名")
    print("-" * 40)
    for m in members:
        print(f"{m.member_id:<20} {m.name}")
    print(f"\n共 {len(members)} 位會員")


def cmd_member_add(db: DBManager, args, _gauge: Optional[WeightGauge] = None) -> None:
    member_id = args.member_id or input(
        f"會員代碼（可直接刷會員條碼；要能登入的話必須以 {_login_prefix()!r} 開頭）："
    ).strip()
    if not member_id:
        print("已取消（沒有輸入會員代碼）。")
        return
    prefix = _login_prefix()
    if prefix and not member_id.startswith(prefix):
        print(
            f"[警告] 這個代碼沒有 {prefix!r} 前綴，登入時會被狀態機判定成"
            "「不是會員碼格式」而拒絕（見 core/cart_state_machine.py 的 _on_login）。"
            "要改的話是改 config.json 的 state_machine.login_barcode_prefix。"
        )
    existing = db.get_member(member_id)
    default_name = existing.name if existing else None
    name = args.name or _prompt("姓名", default_name)
    if not name:
        print("已取消（沒有輸入姓名）。")
        return
    db.upsert_member(Member(member_id=member_id, name=name))
    print(f"{'已更新' if existing else '已新增'}會員：{member_id}　{name}")


def cmd_member_delete(db: DBManager, args, _gauge: Optional[WeightGauge] = None) -> None:
    existing = db.get_member(args.member_id)
    if existing is None:
        print(f"[錯誤] 會員代碼 {args.member_id} 不在資料庫裡，沒有東西可以刪")
        return
    if not args.yes:
        confirm = input(f"確定要刪除會員「{existing.name}」（{existing.member_id}）？輸入 y 確認：").strip().lower()
        if confirm != "y":
            print("已取消。")
            return
    db.delete_member(args.member_id)
    print(f"已刪除會員：{args.member_id}　{existing.name}")


def _login_prefix() -> str:
    """會員條碼前綴，讀 config.json 的 state_machine.login_barcode_prefix。"""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("state_machine", {}).get("login_barcode_prefix", "MEMBER-")
    except Exception:  # noqa: BLE001 — 讀不到設定不該讓整支工具掛掉
        return "MEMBER-"


# ----------------------------------------------------------------------
def _run_menu(db: DBManager, gauge: WeightGauge) -> None:
    while True:
        print(
            "\n=== SmartCart 商品資料庫管理 ===\n"
            "1) 列出所有商品\n"
            "2) 新增商品（條碼可直接刷，重量可直接秤）\n"
            "3) 修改商品\n"
            "4) 刪除商品\n"
            "5) 從 CSV 匯入\n"
            "6) 匯出成 CSV\n"
            "--- 會員 ---\n"
            "7) 列出所有會員\n"
            "8) 新增／修改會員（會員條碼可直接刷）\n"
            "9) 刪除會員\n"
            "0) 離開"
        )
        choice = input("請選擇：").strip()
        if choice == "0":
            return
        elif choice == "1":
            cmd_list(db, None)
        elif choice == "2":
            try:
                cmd_add(db, argparse.Namespace(barcode=None, name=None, price=None, weight=None, tolerance=None), gauge)
            except WeightGaugeUnavailable as exc:
                print(f"\n[錯誤] {exc}\n（其他不需要量重量的選項還是可以用）")
        elif choice == "3":
            barcode = input("要修改哪個條碼：").strip()
            try:
                cmd_edit(db, argparse.Namespace(barcode=barcode), gauge)
            except WeightGaugeUnavailable as exc:
                print(f"\n[錯誤] {exc}\n（其他不需要量重量的選項還是可以用）")
        elif choice == "4":
            barcode = input("要刪除哪個條碼：").strip()
            cmd_delete(db, argparse.Namespace(barcode=barcode, yes=False))
        elif choice == "5":
            path = input("CSV 檔案路徑：").strip()
            cmd_import(db, argparse.Namespace(csv_path=path))
        elif choice == "6":
            path = input("要匯出到哪個檔案路徑：").strip()
            cmd_export(db, argparse.Namespace(csv_path=path))
        elif choice == "7":
            cmd_member_list(db, None)
        elif choice == "8":
            cmd_member_add(db, argparse.Namespace(member_id=None, name=None))
        elif choice == "9":
            member_id = input("要刪除哪個會員代碼：").strip()
            cmd_member_delete(db, argparse.Namespace(member_id=member_id, yes=False))
        else:
            print("看不懂這個選項，請輸入選單裡列出的數字。")


def _load_serial_weight_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    return {
        "port": cfg.get("serial", {}).get("port", "/dev/ttyAMA0"),
        "baudrate": cfg.get("serial", {}).get("baudrate", 115200),
        "offset": cfg.get("weight", {}).get("hx711_offset", 0),
        "scale": cfg.get("weight", {}).get("hx711_scale", 1.0),
    }


def _main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

    serial_cfg = _load_serial_weight_config()

    parser = argparse.ArgumentParser(description="SmartCart 商品資料庫後台管理工具（不加指令會進互動選單）")
    parser.add_argument("--db", default=None, help="資料庫路徑，預設 database/inventory.db")
    parser.add_argument("--port", default=serial_cfg["port"], help=f"秤重感測器 UART 連接埠（預設讀 config.json，目前是 {serial_cfg['port']!r}）")
    parser.add_argument("--baud", type=int, default=serial_cfg["baudrate"], help="UART 鮑率")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="列出所有商品")

    p_add = sub.add_parser("add", help="新增一筆商品（不加參數走互動輸入）")
    p_add.add_argument("--barcode")
    p_add.add_argument("--name")
    p_add.add_argument("--price", type=float)

    p_edit = sub.add_parser("edit", help="修改一筆商品（互動輸入，Enter 保留原值）")
    p_edit.add_argument("barcode")

    p_del = sub.add_parser("delete", help="刪除一筆商品")
    p_del.add_argument("barcode")
    p_del.add_argument("-y", "--yes", action="store_true", help="不詢問直接刪除")

    p_import = sub.add_parser("import", help="從 CSV 批次匯入/更新")
    p_import.add_argument("csv_path")

    p_export = sub.add_parser("export", help="匯出成 CSV")
    p_export.add_argument("csv_path")

    sub.add_parser("member-list", help="列出所有會員")

    p_madd = sub.add_parser("member-add", help="新增／修改會員（不加參數走互動輸入，會員條碼可直接刷）")
    p_madd.add_argument("--member-id")
    p_madd.add_argument("--name")

    p_mdel = sub.add_parser("member-delete", help="刪除會員")
    p_mdel.add_argument("member_id")
    p_mdel.add_argument("-y", "--yes", action="store_true", help="不詢問直接刪除")

    args = parser.parse_args()
    db = DBManager(args.db)
    # seed=False：只建表，不要灌測試資料。這支是「建立正式商品目錄」的後台，
    # 之前寫 seed=True，結果每次對一個全新的資料庫開這支工具，就會先被塞進
    # db_manager.py 裡那 5 筆假的測試商品（統一陽光豆漿、多力多滋…），自己加
    # 的第一筆真商品變成第 6 筆，正式資料跟測試資料混在一起。
    # 要測試資料的話，明確執行 `python3 -m database.db_manager --init`。
    db.init_db(seed=False)

    gauge = WeightGauge(
        port=args.port, baudrate=args.baud,
        offset=serial_cfg["offset"], scale=serial_cfg["scale"],
    )

    handlers = {
        "list": cmd_list, "add": cmd_add, "edit": cmd_edit, "delete": cmd_delete,
        "import": cmd_import, "export": cmd_export,
        "member-list": cmd_member_list, "member-add": cmd_member_add,
        "member-delete": cmd_member_delete,
    }
    try:
        if args.command is None:
            _run_menu(db, gauge)
        else:
            handlers[args.command](db, args, gauge)
    except WeightGaugeUnavailable as exc:
        # 秤連不上是「這次操作做不成」，不是程式壞掉——印乾淨的原因就好，
        # 不要吐一整串 traceback 讓人以為是 bug。
        print(f"\n[錯誤] {exc}\n")
        return 1
    finally:
        gauge.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
