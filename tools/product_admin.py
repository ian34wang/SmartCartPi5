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
10%，一樣不用手動輸入（要自訂的話可以直接覆蓋，見下方欄位說明）。量不到感
測器（沒接 UART、或用這支工具的電腦本來就沒接秤）時會自動退回手動輸入公克
數，不會卡住等不存在的硬體。

用法：
    python3 -m tools.product_admin                        # 互動選單（不熟指令的話用這個）
    python3 -m tools.product_admin list                    # 列出所有商品
    python3 -m tools.product_admin add                     # 互動輸入新增一筆（條碼可直接用掃描器刷）
    python3 -m tools.product_admin add --barcode 471... --name "..." --price 39 --weight 500
        # 命令列參數模式：--tolerance 可以不給，不給就自動抓 --weight 的 10%
    python3 -m tools.product_admin edit 4710018001234       # 互動修改一筆（Enter 保留原值）
    python3 -m tools.product_admin delete 4710018001234
    python3 -m tools.product_admin import products.csv       # 從 CSV 批次匯入/更新
    python3 -m tools.product_admin export products_backup.csv
    python3 -m tools.product_admin add --no-scale            # 不嘗試連秤重感測器，直接手動輸入公克數

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

from database.db_manager import DBManager, Product  # noqa: E402

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
_CSV_FIELDS = ["barcode", "name", "unit_price", "standard_weight_g", "weight_tolerance_g"]
_AUTO_TOLERANCE_RATIO = 0.10  # 自動容差 = 量到的標準重量 × 這個比例


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
    `list`/`delete`/`import`/`export` 這種不需要秤重的指令時，不會平白多等
    好幾秒的連線逾時。連不上（沒接 UART、沒有感測器、這台電腦本來就不是
    Pi）就把 `available` 設成 False，呼叫端要自己 fallback 成手動輸入公克
    數，不能讓整支 CLI 因為秤沒接就用不了。
    """

    def __init__(self, port: str, baudrate: int, offset: float, scale: float,
                 num_samples: int = 20, sample_timeout_sec: float = 3.0,
                 connect_timeout_sec: float = 3.0, disabled: bool = False):
        self.port = port
        self.baudrate = baudrate
        self.offset = offset
        self.scale = scale
        self.num_samples = num_samples
        self.sample_timeout_sec = sample_timeout_sec
        self.connect_timeout_sec = connect_timeout_sec
        self._disabled = disabled
        self._receiver = None
        self._tried = False

    def _ensure_connected(self) -> None:
        if self._tried or self._disabled:
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
                print(f"[警告] 秤重感測器（{self.port}）接上了但沒有收到任何資料，改用手動輸入公克數")
                return
            self._receiver = receiver
        except Exception as exc:  # noqa: BLE001 — 連不上秤是可預期狀況，不該讓 CLI 整支掛掉
            print(f"[警告] 沒有接上秤重感測器（{exc}），改用手動輸入公克數")

    @property
    def available(self) -> bool:
        self._ensure_connected()
        return self._receiver is not None

    def measure(self) -> Optional[float]:
        """回傳這次量到的公克數；沒有連線或沒收到樣本回傳 None。"""
        if not self.available:
            return None
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
            return None
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
    """回傳 (標準重量, 容許誤差)；輸入 q 取消回傳 None。優先走現場量測（把
    商品放上秤按 Enter），量不到或使用者主動要求才退回手動輸入公克數。
    """
    default_w = existing.standard_weight_g if existing else None
    weight: Optional[float] = None
    while weight is None:
        if gauge.available:
            hint = "（輸入 m 改手動輸入公克數"
            hint += f"，k 保留原值 {default_w}g" if default_w is not None else ""
            hint += "，q 取消）"
            raw = input(f"把商品放上秤重感應區，穩定後按 Enter 量測重量{hint}：").strip().lower()
        else:
            raw = "m"  # 沒有秤重連線，直接走手動輸入，不用每次都問一次
        if raw == "q":
            return None
        if raw == "k" and default_w is not None:
            weight = default_w
        elif raw == "m":
            manual = _prompt("標準重量（公克，一件商品的重量）", str(default_w) if default_w is not None else None)
            if manual.lower() == "q":
                return None
            try:
                weight = float(manual)
            except ValueError:
                print("[錯誤] 不是有效數字，請重新輸入")
                continue
        else:
            print("量測中...")
            measured = gauge.measure()
            if measured is None:
                print("[警告] 沒有量到有效讀數（檢查 UART 連線、感應區上是否已經放好商品），請重試，或輸入 m 改手動輸入")
                continue
            weight = measured
            print(f"量到的重量 = {weight:.1f} g")
        if weight is not None and weight <= 0:
            print("[錯誤] 重量必須大於 0，請重新輸入")
            weight = None

    auto_tolerance = round(weight * _AUTO_TOLERANCE_RATIO, 1)
    tol_raw = input(f"容許誤差（公克，直接按 Enter 用自動值 = 重量的 {_AUTO_TOLERANCE_RATIO*100:.0f}% = {auto_tolerance}g；輸入 q 取消）：").strip()
    if tol_raw.lower() == "q":
        return None
    if not tol_raw:
        tolerance = auto_tolerance
    else:
        try:
            tolerance = float(tol_raw)
            if tolerance < 0:
                print("[錯誤] 容許誤差不能是負數，改用自動值")
                tolerance = auto_tolerance
        except ValueError:
            print("[錯誤] 不是有效數字，改用自動值")
            tolerance = auto_tolerance
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
    if args.barcode:  # 非互動模式：全部用命令列參數，name/price 一定要給；
        # weight 也一定要給（現場量測只在互動模式下才有意義），tolerance
        # 可以不給，不給就自動抓 weight 的 10%（跟互動模式邏輯一致）。
        missing = [n for n in ("name", "price", "weight") if getattr(args, n) is None]
        if missing:
            print(f"[錯誤] --barcode 有給的話，{missing} 也都要一起給（或乾脆不加 --barcode 走互動輸入）")
            return
        tolerance = args.tolerance if args.tolerance is not None else round(args.weight * _AUTO_TOLERANCE_RATIO, 1)
        try:
            product = parse_product_row({
                "barcode": args.barcode, "name": args.name, "unit_price": args.price,
                "standard_weight_g": args.weight, "weight_tolerance_g": tolerance,
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
            "0) 離開"
        )
        choice = input("請選擇：").strip()
        if choice == "0":
            return
        elif choice == "1":
            cmd_list(db, None)
        elif choice == "2":
            cmd_add(db, argparse.Namespace(barcode=None, name=None, price=None, weight=None, tolerance=None), gauge)
        elif choice == "3":
            barcode = input("要修改哪個條碼：").strip()
            cmd_edit(db, argparse.Namespace(barcode=barcode), gauge)
        elif choice == "4":
            barcode = input("要刪除哪個條碼：").strip()
            cmd_delete(db, argparse.Namespace(barcode=barcode, yes=False))
        elif choice == "5":
            path = input("CSV 檔案路徑：").strip()
            cmd_import(db, argparse.Namespace(csv_path=path))
        elif choice == "6":
            path = input("要匯出到哪個檔案路徑：").strip()
            cmd_export(db, argparse.Namespace(csv_path=path))
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
    parser.add_argument("--no-scale", action="store_true", help="不嘗試連秤重感測器，重量欄位一律手動輸入公克數")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="列出所有商品")

    p_add = sub.add_parser("add", help="新增一筆商品（不加參數走互動輸入）")
    p_add.add_argument("--barcode")
    p_add.add_argument("--name")
    p_add.add_argument("--price", type=float)
    p_add.add_argument("--weight", type=float, help="標準重量（公克）")
    p_add.add_argument("--tolerance", type=float, help=f"容許誤差（公克）；不給的話自動抓 --weight 的 {_AUTO_TOLERANCE_RATIO*100:.0f}%%")

    p_edit = sub.add_parser("edit", help="修改一筆商品（互動輸入，Enter 保留原值）")
    p_edit.add_argument("barcode")

    p_del = sub.add_parser("delete", help="刪除一筆商品")
    p_del.add_argument("barcode")
    p_del.add_argument("-y", "--yes", action="store_true", help="不詢問直接刪除")

    p_import = sub.add_parser("import", help="從 CSV 批次匯入/更新")
    p_import.add_argument("csv_path")

    p_export = sub.add_parser("export", help="匯出成 CSV")
    p_export.add_argument("csv_path")

    args = parser.parse_args()
    db = DBManager(args.db)
    db.init_db(seed=True)  # 確保表存在；資料庫已經有資料的話 seed 不會覆蓋

    gauge = WeightGauge(
        port=args.port, baudrate=args.baud,
        offset=serial_cfg["offset"], scale=serial_cfg["scale"],
        disabled=args.no_scale,
    )

    handlers = {
        "list": cmd_list, "add": cmd_add, "edit": cmd_edit, "delete": cmd_delete,
        "import": cmd_import, "export": cmd_export,
    }
    try:
        if args.command is None:
            _run_menu(db, gauge)
        else:
            handlers[args.command](db, args, gauge)
    finally:
        gauge.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
