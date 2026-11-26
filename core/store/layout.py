"""
core/store/layout.py

店面平面圖（找貨頁的地圖、商品貨架位置、推車位置換算）。照 2026-11-26 架好的實際場地
（docs/store_field.png、docs/store_field.DWG，CAD 圖，單位 mm）：

            ┌──────────────────────────────────────┐  y = 5.195（後牆）
            │                                      │
            │      ┌────┐            ┌────┐        │  y = 4.285
            │      │左架│            │右架│        │
            │      │    │            │    │        │
            │      └────┘            └────┘        │  y = 3.065
            │                                      │
            └────────────────┐    ┌────────────────┘  y = 2.45（大廳前緣 = 長廊末端）
                             │ ◇  │                   y = 2.20 定位點標記
                             │長廊│
                             └─門─┘                   y = 0（管制區閘門，門內／門外的分界）
          x = −2.06        x = −0.31 ~ +0.31          x = +2.09

座標單位是**公尺**，原點在門（長廊入口）的正中間，y 往店裡、x 往右。從 CAD 圖換算：
    大廳   4.150 × 2.745 m；長廊 0.620 × 2.450 m，左緣離大廳左牆 1.750 m（右緣離右牆 1.780 m）
    左架   左緣離大廳左牆 0.930 m，0.640 × 1.220 m；兩架之間 1.020 m；右架右緣離右牆 0.920 m
    兩架   前緣離大廳前緣 0.615 m、後緣離後牆 0.910 m
場地改了就改 DEFAULT_LAYOUT（或 config.json 的 store 區塊覆蓋），validate() 會擋掉不合理的數字。

貨架位置代碼：貨架代號 + 位置編號，例如 "L3" = 左架左側靠門那段。位置是貨架的某一面（face：
左／右／前／後，「前」是靠門那面）上的某一段（at：沿那一面從前／左算起的比例，0~1），推車停在那一面往外
slot_margin_m 的地方。預設每座貨架 4 個位置（兩座都是長邊朝左右，商品擺在長邊）：
    1 左側・後段  2 右側・後段  3 左側・前段  4 右側・前段
要改分法（例如只用前後兩面），改 config.json 的 store.slots 就好，程式不用動。

長廊與定位點：
    長廊   鎖住移動方向：推車在長廊裡只能朝店內直走，所以航向就是平面圖的 +y、橫向位置就是長廊中線。
    定位點 長廊中線、靠末端的地上貼一張 ArUco 標記（core/store/vision.py）。相機看到它就算出標記在推車
           前方、右方各幾公尺、上緣朝哪，反推推車在平面圖上的位置與航向，連接「大圖座標」（這張平面圖）與
           「內部定位座標」（里程計）。標記可以貼在店裡任何地方（交叉口最好：推車在那裡轉彎），可以貼很多張
           （markers），從哪個方向看到都對（pose_from_marker）。

路線：core/store/navigation.py 在格點上用 A* 找推車中心的最短路徑（離牆、貨架至少 nav_clearance_m），再拉直。

推車位置：里程計 (X, Y)（mm，原點與方向是開機時決定的）換成平面圖 (x, y)（m）：
        [x]      1    [ cos α  −sin α ] [X − X₀]   [x₀]
        [y]  =  ────  [ sin α   cos α ] [Y − Y₀] + [y₀]
                1000
    (X₀, Y₀) 對齊那一刻的里程計位置，(x₀, y₀) 那一刻推車在平面圖上的位置。
    α 是里程計航向 → 平面圖航向的差：平面圖航向 θ = ψ + α，ψ 是里程計（IMU）航向。

三個時間點：
    進場（BLE 判定）   enter()：推車在長廊、車頭朝店內 → α = 90° − ψ，(x₀, y₀) = (0, entry_y)。
                       之後看到標記之前，x 鎖在長廊中線、y 最多到長廊末端（長廊只能直走），地圖上是空心點（約略）。
                       entry_y 是 BLE 判定出進場時推車大約在長廊哪裡（判定比穿越晚 1~2 秒，預設 0.8 m）。
    看到定位點標記     anchor()：α = 90° − ψ（航向固定成正向），(x₀, y₀) = 定位點 − 標記相對推車的
                       (右方, 前方)（core/store/vision.py 由影像算出）。實心點。
                       登入時推車已經在店裡（沒有進場點）也一樣：推回長廊讓相機看到標記就對齊；
                       找貨頁也有「我在定位點」按鈕可以手動對齊。
    之後一直           correct_heading()：貨架邊緣的消失點量到平面圖航向 θ_v（core/store/vision.py），
                       跟目前估計 θ̂ = ψ + α 比，誤差 e = θ_v − θ̂，|e| ≤ max_err_deg 才採用：
                           α ← α + w·e          （w = vision.vp_yaw_weight，互補濾波）
                       以推車目前位置為中心轉（畫面上的點不跳，之後的位移方向變準）。
誤差：長廊 0.62 m 寬，對齊時推車中心橫向最多偏約 ±0.1 m（車寬佔掉大半）；之後位移量隨里程計累積
（直線實測 ±2%），航向由消失點拉回。
"""

from __future__ import annotations

import copy
import math
from typing import Dict, List, Optional, Tuple

from core.angles import wrap_deg

DEFAULT_LAYOUT: Dict = {
    "hall": [-2.06, 2.09, 2.45, 5.195],     # x0, x1, y0, y1（m）
    "corridor": [-0.31, 0.31, 0.0, 2.45],
    # 主定位點：長廊裡、編號 0、上緣朝店內（+y）。其他轉角的標記寫在 markers（config.json 的 store.markers）
    "anchor": {"name": "定位點", "x": 0.0, "y": 2.20, "id": 0, "up_deg": 90.0},
    "markers": [],
    "entry_y": 0.8,
    "slot_margin_m": 0.3,
    "arrive_radius_m": 0.3,     # 推車離停車點這麼近就算到達，自動結束導航（ui/bridge.py push_pos）
    "nav_clearance_m": 0.2,                 # 路徑規劃：推車中心離牆、貨架至少這麼遠（core/store/navigation.py）
    "shelves": [
        {"id": "L", "name": "左架", "rect": [-1.13, -0.49, 3.065, 4.285]},
        {"id": "R", "name": "右架", "rect": [0.53, 1.17, 3.065, 4.285]},
    ],
    "slots": {
        "1": {"name": "左側・後段", "face": "左", "at": 0.75},
        "2": {"name": "右側・後段", "face": "右", "at": 0.75},
        "3": {"name": "左側・前段", "face": "左", "at": 0.25},
        "4": {"name": "右側・前段", "face": "右", "at": 0.25},
    },
}

_FACES = ("左", "右", "前", "後")


def load(store_cfg: Optional[dict] = None) -> Dict:
    """預設平面圖，套上 config.json 的 store 區塊（有給的欄位覆蓋，底線開頭的註解忽略）。"""
    lay = copy.deepcopy(DEFAULT_LAYOUT)
    for k, v in (store_cfg or {}).items():
        if not k.startswith("_"):
            lay[k] = v
    return lay


def _rect_ok(r) -> bool:
    try:
        x0, x1, y0, y1 = (float(v) for v in r)
    except (TypeError, ValueError):
        return False
    return x0 < x1 and y0 < y1


def _inside(p, r, eps: float = 1e-9) -> bool:
    return r[0] - eps <= p[0] <= r[1] + eps and r[2] - eps <= p[1] <= r[3] + eps


def validate(lay: Dict) -> None:
    """平面圖不合理就丟 ValueError（ui/app_gui.py 啟動時呼叫，不拿錯的平面圖畫推車位置）。"""
    errs = []
    for key in ("hall", "corridor"):
        if not _rect_ok(lay.get(key)):
            errs.append(f"{key} 要是 [x0, x1, y0, y1] 而且 x0 < x1、y0 < y1：{lay.get(key)!r}")
    if errs:
        raise ValueError("；".join(errs))
    hall, cor = lay["hall"], lay["corridor"]
    if abs(cor[3] - hall[2]) > 1e-6:
        errs.append(f"長廊末端 y = {cor[3]} 要接在大廳前緣 y = {hall[2]}")
    if not (hall[0] <= cor[0] and cor[1] <= hall[1]):
        errs.append("長廊要在大廳左右範圍內")
    a = lay.get("anchor") or {}
    try:
        ap = (float(a["x"]), float(a["y"]))
    except (KeyError, TypeError, ValueError):
        errs.append(f"anchor 要有 x、y：{a!r}")
    else:
        if not (_inside(ap, cor) or _inside(ap, hall)):
            errs.append(f"定位點 {ap} 不在店裡")
    seen = set()
    for m in lay.get("markers") or []:
        try:
            mid, p = int(m["id"]), (float(m["x"]), float(m["y"]))
            float(m.get("up_deg", 90.0))
        except (KeyError, TypeError, ValueError):
            errs.append(f"markers 每一張要有 id、x、y（up_deg 選填，預設 90）：{m!r}")
            continue
        if not (_inside(p, hall) or _inside(p, cor)):
            errs.append(f"標記 {mid} 的位置 {p} 不在店裡")
        seen.add(mid)
    try:
        aid = int((lay.get("anchor") or {}).get("id", 0))
    except (TypeError, ValueError):
        aid = None
        errs.append("anchor.id 要是整數")
    ids = [aid] + [int(m["id"]) for m in lay.get("markers") or [] if str(m.get("id", "")).lstrip("-").isdigit()]
    if len(ids) != len(set(ids)):
        errs.append(f"標記編號重複了：{ids}（每一張要不同編號，tools/make_anchor_marker.py --id 產生）")
    if any(i is not None and not 0 <= i < 50 for i in ids):
        errs.append(f"標記編號要在 0~49（DICT_4X4_50）：{ids}")
    try:
        ey = float(lay.get("entry_y"))
    except (TypeError, ValueError):
        errs.append(f"entry_y 要是數字：{lay.get('entry_y')!r}")
    else:
        if not cor[2] <= ey <= cor[3]:
            errs.append(f"entry_y = {ey} 要在長廊範圍 {cor[2]} ~ {cor[3]} 內")
    m = lay.get("slot_margin_m")
    if not isinstance(m, (int, float)) or m <= 0:
        errs.append(f"slot_margin_m 要是正數：{m!r}")
    r = lay.get("arrive_radius_m", 0.3)
    if not isinstance(r, (int, float)) or r <= 0:
        errs.append(f"arrive_radius_m 要是正數：{r!r}")
    c = lay.get("nav_clearance_m", 0.2)
    if not isinstance(c, (int, float)) or c < 0 or c * 2 >= cor[1] - cor[0]:
        errs.append(f"nav_clearance_m 要 ≥ 0 而且小於長廊寬的一半（不然推車過不了長廊）：{c!r}")
    for s in lay.get("shelves") or []:
        if not (s.get("id") and s.get("name") and _rect_ok(s.get("rect"))):
            errs.append(f"貨架格式不對：{s!r}")
        elif not (_inside(s["rect"][0::2], hall) and _inside(s["rect"][1::2], hall)):
            errs.append(f"{s['name']} 超出大廳")
    for k, spec in (lay.get("slots") or {}).items():
        if not (isinstance(spec, dict) and spec.get("name") and spec.get("face") in _FACES
                and isinstance(spec.get("at", 0.5), (int, float)) and 0 <= spec.get("at", 0.5) <= 1):
            errs.append(f"slots.{k} 要是 {{name, face（左/右/前/後）, at（0~1）}}：{spec!r}")
    if not errs:
        for c in codes(lay):
            p = slot_point(lay, c)
            if not _inside(p, hall):
                errs.append(f"{c} 的停車點 {p} 在大廳外（slot_margin_m 太大或貨架太靠牆）")
    if errs:
        raise ValueError("；".join(errs))


def markers(lay: Dict) -> List[Dict]:
    """店裡全部的定位標記：主定位點（anchor）加上 markers，每張 {id, name, x, y, up_deg}。
    up_deg = 標記**上緣**（印出來箭頭那一邊）在平面圖上指的方向（度，+x 為 0、逆時針為正，朝店內 = 90）。"""
    a = lay["anchor"]
    out = [{"id": int(a.get("id", 0)), "name": a.get("name", "定位點"), "x": float(a["x"]), "y": float(a["y"]),
            "up_deg": float(a.get("up_deg", 90.0))}]
    for m in lay.get("markers") or []:
        out.append({"id": int(m["id"]), "name": m.get("name") or f"標記 {m['id']}", "x": float(m["x"]),
                    "y": float(m["y"]), "up_deg": float(m.get("up_deg", 90.0))})
    return out


def marker_by_id(lay: Dict, marker_id: int) -> Optional[Dict]:
    for m in markers(lay):
        if m["id"] == int(marker_id):
            return m
    return None


def pose_from_marker(marker: Dict, forward_m: float, lateral_m: float, marker_dir_deg: float,
                     cam_forward_m: float = 0.0, cam_right_m: float = 0.0) -> Tuple[float, float, float]:
    """看到一張標記 → 推車（光流感測器那一點）在平面圖上的位置與航向。

    令標記中心在平面圖上是 M、上緣方向 θ_m（up_deg）；相機量到標記在前方 ℓ_f、右方 ℓ_r，標記上緣在
    車頭方向右邊 φ 度（core/store/vision.floor_pose）。則
        θ = θ_m + φ                                  推車航向（度，+x 為 0、逆時針為正）
        F = (cos θ, sin θ)，  Q = (sin θ, −cos θ)     車頭方向、車身右方（平面圖上的單位向量）
        C = M − ℓ_f F − ℓ_r Q                        相機正下方那一點
        S = C − f_c F − r_c Q                        光流感測器（f_c、r_c = 相機在它前方、右方幾公尺）
    回傳 (S_x, S_y, θ)。
    """
    th = wrap_deg(float(marker["up_deg"]) + float(marker_dir_deg))
    c, s_ = math.cos(math.radians(th)), math.sin(math.radians(th))
    fwd = float(forward_m) + float(cam_forward_m)
    right = float(lateral_m) + float(cam_right_m)
    return (float(marker["x"]) - fwd * c - right * s_, float(marker["y"]) - fwd * s_ + right * c, th)


def codes(lay: Dict) -> List[str]:
    return [s["id"] + k for s in lay["shelves"] for k in lay["slots"]]


def normalize(code: str) -> str:
    return (code or "").strip().upper()


def is_valid(lay: Dict, code: str) -> bool:
    return normalize(code) in codes(lay)


def _find(lay: Dict, code: str) -> Tuple[Optional[dict], Optional[dict]]:
    code = normalize(code)
    for s in lay["shelves"]:
        k = code[len(s["id"]):]
        if code.startswith(s["id"]) and k in lay["slots"]:
            return s, lay["slots"][k]
    return None, None


def describe(lay: Dict, code: str) -> str:
    """"L3" → "左架・左側・前段"。"""
    s, spec = _find(lay, code)
    return normalize(code) if s is None else f"{s['name']}・{spec['name']}"


def slot_point(lay: Dict, code: str) -> Optional[Tuple[float, float]]:
    """推車停的那一點：貨架那一面往外 slot_margin_m、沿那一面 at 的位置。"""
    s, spec = _find(lay, code)
    if s is None:
        return None
    x0, x1, y0, y1 = (float(v) for v in s["rect"])
    m, t, face = float(lay["slot_margin_m"]), float(spec.get("at", 0.5)), spec["face"]
    if face in ("左", "右"):
        x, y = (x0 - m if face == "左" else x1 + m), y0 + t * (y1 - y0)
    else:
        x, y = x0 + t * (x1 - x0), (y0 - m if face == "前" else y1 + m)
    return round(x, 3), round(y, 3)


def route(lay: Dict, code: str, start: Optional[Tuple[float, float]] = None) -> List[Tuple[float, float]]:
    """從 start（預設門口正中間）到這個位置停車點的路線（m），A* 避開牆與貨架（core/store/navigation.py）。
    地圖上畫虛線用；推車位置已知時 ui/bridge.py 會從推車目前位置重算。走不到回 []。"""
    from core.store.navigation import planner_for

    p = slot_point(lay, code)
    if p is None:
        return []
    cor = lay["corridor"]
    s0 = start if start is not None else ((float(cor[0]) + float(cor[1])) / 2, float(cor[2]))
    return planner_for(lay).plan(s0, p)


def instruction(lay: Dict, code: str) -> str:
    s, spec = _find(lay, code)
    if s is None:
        return ""
    cx = (float(s["rect"][0]) + float(s["rect"][1])) / 2
    where = "往左走到" if cx < -0.2 else "往右走到" if cx > 0.2 else "正前方就是"
    face, t = spec["face"], float(spec.get("at", 0.5))
    if face in ("左", "右"):
        part = "靠門那段" if t < 0.5 else "靠後牆那段" if t > 0.5 else "中間"
        side = f"在貨架{face}側、{part}"
    else:
        part = "左邊" if t < 0.5 else "右邊" if t > 0.5 else "中間"
        side = "在貨架前側（靠門）" if face == "前" else "繞到貨架後側（靠後牆）"
        side += f"、{part}"
    return f"進門沿長廊直走過{lay['anchor']['name']} → {where}{s['name']} → {side}"


def to_client(lay: Dict) -> Dict:
    """給前端畫地圖用（純資料）。"""
    out = {k: lay[k] for k in ("hall", "corridor", "anchor", "shelves", "slots")}
    out["markers"] = markers(lay)[1:]          # 主定位點以外的標記（地圖上畫小菱形）
    return out


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
        self._offset = (0.0, 0.0)                             # x₀, y₀（m）
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
        self._offset = (cx, float(self.lay["entry_y"]))
        self.status = self.ROUGH

    def anchor(self, x_mm: float, y_mm: float, yaw_deg: float,
               at: Optional[Tuple[float, float]] = None, heading: Optional[float] = None) -> None:
        """at = 推車此刻在平面圖上的位置（m）；不給就是正好在主定位點上（「我在定位點」按鈕）。
        heading = 推車此刻在平面圖上的航向（度）；不給就當成朝店內（90°，在長廊裡）。"""
        a = self.lay["anchor"]
        self._origin = (x_mm, y_mm)
        self._alpha = (90.0 if heading is None else float(heading)) - yaw_deg
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
        return ((math.cos(a) * dx - math.sin(a) * dy) / 1000.0 + self._offset[0],
                (math.sin(a) * dx + math.cos(a) * dy) / 1000.0 + self._offset[1])

    def to_store(self, x_mm: float, y_mm: float) -> Optional[Tuple[float, float]]:
        if self._origin is None:
            return None
        sx, sy = self._raw(x_mm, y_mm)
        cx0, cx1, cy0, _ = self.lay["corridor"]
        if self.status == self.ROUGH:      # 長廊鎖住方向：還沒到定位點前只能在中線上前後
            sx = (cx0 + cx1) / 2
            return sx, max(cy0 - 0.5, min(float(self.lay["corridor"][3]), sy))
        hx0, hx1, _, hy1 = self.lay["hall"]
        return max(hx0, min(hx1, sx)), max(cy0 - 0.5, min(hy1, sy))
