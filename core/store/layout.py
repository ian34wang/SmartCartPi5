"""
core/store/layout.py

店面平面圖（找貨頁的地圖、商品貨架位置、推車位置換算）。照 2026-11-23 的構想圖：

            ┌────────────────────────────────┐  y = 8（後牆）
            │   ┌──┐     ┌──────┐     ┌──┐   │
            │   │左│     │  中  │     │右│   │  y = 5 ~ 7（貨架）
            │   └──┘     └──────┘     └──┘   │
            │             [定位點]           │  y ≈ 4.2
            └──────────┐          ┌──────────┘  y = 4（大廳前緣）
                       │  長廊    │              x = −1 ~ +1（機器視覺校正用長廊）
                       │    ↑     │
                       └── 門 ────┘              y = 0（管制區閘門，門內／門外的分界）
              x = −4                      x = +4

座標單位是「格」（構想圖方格紙的一格），原點在門的正中間，y 往店裡、x 往右。
一格多長是 unit_m（公尺），推車位置要用，**要現場量**（config.json 的 store.unit_m）。沒有預設值：
沒填、或不是正數，unit_m() 丟 ValueError，ui/app_gui.py 啟動時就報錯結束（不拿沒量過的數字畫位置）。
量法：長廊寬 W_c（m）是 2 格，u = W_c / 2；門到定位點標記中心 d（m）是 4.25 格，u = d / 4.25，兩個應該接近。

貨架位置代碼：貨架代號 + 角落編號，例如 "M3" = 中架的左下角。構想圖上每座貨架的四個角都有畫記號，
所以預設每座貨架四個位置：1 左上、2 右上、3 左下、4 右下（「上」是靠後牆那側，「下」是靠定位點那側）。
要改成別的分法（例如前面／後面兩側），改 config.json 的 store.slots 就好，程式不用動。

長廊與定位點（2026-11-23 構想圖的說明）：
    長廊   鎖住移動方向：推車在長廊裡只能朝店內直走，所以航向就是平面圖的 +y、橫向位置就是長廊中線。
    定位點 長廊末端，地上貼一張 ArUco 標記（core/store/vision.py）。相機看到它就算出標記在推車
           前方、右方各幾公尺，反推推車在平面圖上的位置，連接「大圖座標」（這張平面圖）與
           「內部定位座標」（里程計）。

推車位置：里程計 (X, Y)（mm，原點與方向是開機時決定的）換成平面圖 (x, y)（格）：
        [x]          1     [ cos α  −sin α ] [X − X₀]   [x₀]
        [y]  =  ─────────  [ sin α   cos α ] [Y − Y₀] + [y₀]
                 1000·u
    (X₀, Y₀) 對齊那一刻的里程計位置，(x₀, y₀) 那一刻推車在平面圖上的位置，u = unit_m（一格幾公尺）。
    α 是里程計航向 → 平面圖航向的差：平面圖航向 θ = ψ + α，ψ 是里程計（IMU）航向。

三個時間點：
    進場（BLE 判定）   enter()：推車在長廊、車頭朝店內 → α = 90° − ψ，(x₀, y₀) = (0, entry_y)。
                       之後到定位點之前，x 鎖在長廊中線（長廊只能直走），地圖上是空心點（約略）。
                       entry_y 是 BLE 判定出進場時推車大約在長廊哪裡（判定比穿越晚 1~2 秒，預設 1 格）。
    看到定位點標記     anchor()：α = 90° − ψ（航向固定成正向），(x₀, y₀) = 定位點 − 標記相對推車的
                       (右方, 前方)（標記貼在地上的定位點，core/store/vision.py 由影像算出）。實心點。
                       登入時推車已經在店裡（沒有進場點）也一樣：推到定位點前面看到標記就對齊；
                       找貨頁也有「我在定位點」按鈕可以手動對齊。
    之後一直           correct_heading()：貨架邊緣的消失點量到平面圖航向 θ_v（core/store/vision.py），
                       跟目前估計 θ̂ = ψ + α 比，誤差 e = θ_v − θ̂，|e| ≤ max_err_deg 才採用：
                           α ← α + w·e          （w = vision.vp_yaw_weight，互補濾波）
                       以推車目前位置為中心轉（畫面上的點不跳，之後的位移方向變準）。
誤差：長廊 2 格寬，定位點橫向最多差 1 格；位移量隨里程計累積（直線實測 ±2%），航向由消失點拉回。
"""

from __future__ import annotations

import copy
import math
from typing import Dict, List, Optional, Tuple

from core.angles import wrap_deg

DEFAULT_LAYOUT: Dict = {
    "unit_m": None,                 # 現場量，config.json 的 store.unit_m
    "hall": [-4, 4, 4, 8],          # x0, x1, y0, y1
    "corridor": [-1, 1, 0, 4],
    "anchor": {"name": "定位點", "x": 0, "y": 4.25},
    "entry_y": 1.0,
    "shelves": [
        {"id": "L", "name": "左架", "rect": [-3, -2, 5, 7]},
        {"id": "M", "name": "中架", "rect": [-1, 1, 5, 7]},
        {"id": "R", "name": "右架", "rect": [2, 3, 5, 7]},
    ],
    "slots": {"1": "左上", "2": "右上", "3": "左下", "4": "右下"},
}


def load(store_cfg: Optional[dict] = None) -> Dict:
    """預設平面圖，套上 config.json 的 store 區塊（有給的欄位覆蓋，底線開頭的註解忽略）。"""
    lay = copy.deepcopy(DEFAULT_LAYOUT)
    for k, v in (store_cfg or {}).items():
        if not k.startswith("_"):
            lay[k] = v
    return lay


def unit_m(lay: Dict) -> float:
    """一格幾公尺。沒填或不是正數就丟 ValueError。"""
    u = lay.get("unit_m")
    try:
        u = float(u)
    except (TypeError, ValueError):
        u = 0.0
    if not (u > 0 and math.isfinite(u)):
        raw = lay.get("unit_m")
        state = "還沒填" if raw is None else f"是 {raw!r}，要正數"
        raise ValueError(
            f"config.json 的 store.unit_m {state}：平面圖一格幾公尺，要現場量"
            "（長廊寬 ÷ 2，或門到定位點標記中心的距離 ÷ 4.25）")
    return u


def codes(lay: Dict) -> List[str]:
    return [s["id"] + k for s in lay["shelves"] for k in lay["slots"]]


def normalize(code: str) -> str:
    return (code or "").strip().upper()


def is_valid(lay: Dict, code: str) -> bool:
    return normalize(code) in codes(lay)


def _shelf(lay: Dict, code: str) -> Optional[dict]:
    code = normalize(code)
    for s in lay["shelves"]:
        if code.startswith(s["id"]) and code[len(s["id"]):] in lay["slots"]:
            return s
    return None


def describe(lay: Dict, code: str) -> str:
    """"M3" → "中架・左下"。"""
    s = _shelf(lay, code)
    if s is None:
        return normalize(code)
    return f"{s['name']}・{lay['slots'][normalize(code)[len(s['id']):]]}"


def slot_point(lay: Dict, code: str) -> Optional[Tuple[float, float]]:
    """這個位置在貨架外面、推車停的那一點（角落往外 0.5 格）。"""
    s = _shelf(lay, code)
    if s is None:
        return None
    x0, x1, y0, y1 = s["rect"]
    name = lay["slots"][normalize(code)[len(s["id"]):]]
    x = x0 if "左" in name else x1 if "右" in name else (x0 + x1) / 2
    y = y1 + 0.5 if "上" in name else y0 - 0.5 if "下" in name else (y0 + y1) / 2
    return x, y


def route(lay: Dict, code: str) -> List[Tuple[float, float]]:
    """門 → 定位點 → 貨架前的走道 → （上側的話繞到貨架後面）→ 目標。地圖上畫虛線用。"""
    s = _shelf(lay, code)
    p = slot_point(lay, code)
    if s is None or p is None:
        return []
    a = lay["anchor"]
    x0, x1, y0, y1 = s["rect"]
    front_y = y0 - 0.5
    pts = [(0.0, 0.0), (a["x"], a["y"])]
    if p[1] > y1:          # 目標在貨架後側：走到貨架那一側的外面，沿著往後繞
        side_x = x0 - 0.5 if p[0] <= (x0 + x1) / 2 else x1 + 0.5
        pts += [(side_x, front_y), (side_x, p[1]), p]
    else:
        pts.append(p)
    return pts


def instruction(lay: Dict, code: str) -> str:
    s = _shelf(lay, code)
    if s is None:
        return ""
    cx = (s["rect"][0] + s["rect"][1]) / 2
    where = "往左走到" if cx < -0.5 else "往右走到" if cx > 0.5 else "正前方就是"
    name = lay["slots"][normalize(code)[len(s["id"]):]]
    side = "繞到貨架後側（靠後牆）" if "上" in name else "在貨架前側（靠定位點）"
    corner = "左邊那角" if "左" in name else "右邊那角" if "右" in name else "中間"
    return f"進門沿長廊直走到{lay['anchor']['name']} → {where}{s['name']} → {side}{corner}"


def to_client(lay: Dict) -> Dict:
    """給前端畫地圖用（純資料）。"""
    return {k: lay[k] for k in ("hall", "corridor", "anchor", "shelves", "slots")}


class StoreFrame:
    """把里程計座標換成店面座標（公式見檔案開頭）。

    enter()            BLE 判定進場：暫時對齊（長廊、朝店內）
    anchor()           看到定位點標記（或按「我在定位點」）：正式對齊
    correct_heading()  消失點量到的航向偏差，持續修正
    feed()             每筆里程計位置都餵進來，回傳店面座標（也存在 .last）
    clear()            登出、走出管制區
    """

    NONE, ROUGH, ANCHORED = "none", "rough", "anchored"

    def __init__(self, lay: Dict, max_err_deg: float = 15.0):
        self.lay = lay
        self.max_err_deg = max_err_deg
        self.clear()

    def clear(self) -> None:
        self.status = self.NONE
        self._origin: Optional[Tuple[float, float]] = None   # X₀, Y₀ (mm)
        self._alpha = 0.0                                     # 度
        self._offset = (0.0, 0.0)                             # x₀, y₀（格）
        self.last: Optional[Tuple[float, float]] = None

    @property
    def aligned(self) -> bool:
        return self.status != self.NONE

    def heading(self, yaw_deg: float) -> Optional[float]:
        """平面圖上的航向 θ = ψ + α（度，+x 為 0、逆時針為正；朝店內 = 90）。"""
        return None if self._origin is None else wrap_deg(yaw_deg + self._alpha)

    def enter(self, x_mm: float, y_mm: float, yaw_deg: float) -> None:
        self._origin = (x_mm, y_mm)
        self._alpha = 90.0 - yaw_deg
        cx = (self.lay["corridor"][0] + self.lay["corridor"][1]) / 2
        self._offset = (cx, float(self.lay.get("entry_y", 1.0)))
        self.status = self.ROUGH

    def anchor(self, x_mm: float, y_mm: float, yaw_deg: float,
               at: Optional[Tuple[float, float]] = None) -> None:
        """at = 推車此刻在平面圖上的位置（格）；不給就是正好在定位點上（「我在定位點」按鈕）。"""
        a = self.lay["anchor"]
        self._origin = (x_mm, y_mm)
        self._alpha = 90.0 - yaw_deg
        self._offset = at if at is not None else (float(a["x"]), float(a["y"]))
        self.status = self.ANCHORED

    def correct_heading(self, x_mm: float, y_mm: float, yaw_deg: float, measured_deg: float,
                        weight: float) -> Optional[float]:
        """measured_deg = 消失點量到的平面圖航向 θ_v。回傳採用的誤差 e（度），沒採用回 None。"""
        if self.status != self.ANCHORED or weight <= 0:
            return None
        e = wrap_deg(measured_deg - (yaw_deg + self._alpha))
        if abs(e) > self.max_err_deg:
            return None
        here = self._raw(x_mm, y_mm)
        self._origin, self._offset = (x_mm, y_mm), here
        self._alpha += min(1.0, weight) * e
        return e

    def feed(self, x_mm: float, y_mm: float) -> Optional[Tuple[float, float]]:
        self.last = self.to_store(x_mm, y_mm)
        return self.last

    def _raw(self, x_mm: float, y_mm: float) -> Tuple[float, float]:
        x0, y0 = self._origin
        a = math.radians(self._alpha)
        dx, dy = x_mm - x0, y_mm - y0
        unit_mm = 1000.0 * unit_m(self.lay)
        return ((math.cos(a) * dx - math.sin(a) * dy) / unit_mm + self._offset[0],
                (math.sin(a) * dx + math.cos(a) * dy) / unit_mm + self._offset[1])

    def to_store(self, x_mm: float, y_mm: float) -> Optional[Tuple[float, float]]:
        if self._origin is None:
            return None
        sx, sy = self._raw(x_mm, y_mm)
        if self.status == self.ROUGH:      # 長廊鎖住方向：還沒到定位點前只能在中線上前後
            cx0, cx1, cy0, _ = self.lay["corridor"]
            sx = (cx0 + cx1) / 2
            return sx, max(cy0 - 1.0, min(float(self.lay["anchor"]["y"]), sy))
        hx0, hx1, _, hy1 = self.lay["hall"]
        return max(hx0, min(hx1, sx)), max(-1.0, min(hy1, sy))
