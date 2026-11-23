"""
core/store/vision.py

找貨頁推車位置的機器視覺部分（相機 + OpenCV）。純函式／純邏輯，輸入是一張灰階影像，
不碰硬體；擷取影格在 ui/workers.py 的 VisionConsumer。

兩件事：

座標約定：相機座標 x 右、y 下、z 沿光軸往前。相機裝在約 50 cm 高、往斜前方（往下）打。

1. 定位點偵測（AnchorDetector）
   定位點是一張貼在地上的 ArUco 標記（tools/make_anchor_marker.py 產生，邊長 s = vision.anchor_marker_size_m）。
   標記的四個角 → cv2.solvePnP（IPPE_SQUARE，平面正方形專用）→ 標記在相機座標裡的旋轉 R 與位置 t：
       t   相機 → 標記中心的向量（m）
       n   R 的第三欄 = 標記面的法向量，貼在地上所以就是「往上」
   拆成垂直與水平兩部分：
       h = −n·t                     相機離地高度（拿來檢查：跟 vision.camera_height_m 差太多就不採用）
       g = t − (t·n) n              相機正下方 → 標記中心，貼地的水平向量
       f = normalize(ẑ − (ẑ·n) n)   光軸投影到地面 = 車頭方向（ẑ = (0, 0, 1)）
       r = f × n                    車身右方
       forward = g·f，lateral = g·r  標記在推車前方幾公尺、右方幾公尺
   不需要知道相機的俯角：地面方向直接由標記本身量出來。
   ui/bridge.py：forward、lateral 換成格數，推車位置 = 定位點 − (lateral, forward)（航向固定朝店內，
   右方 = +x、前方 = +y）。水平距離 √(forward² + lateral²) ≤ vision.anchor_trigger_m 才採用（越近越準）。

2. 消失點航向（vanishing_point + vp_offset_deg + heading_from_vp）
   貨架都跟平面圖的座標軸平行，所以貨架的水平邊（層板、上下緣，還有地磚縫）在影像上交於一個消失點 (u, v)。
   相機往下俯 β，一個跟光軸水平夾角 γ 的水平方向（往右為正）在相機座標是
       (sin γ,  −cos γ sin β,  cos γ cos β)
   投影：
       u = c_x + f_x · tan γ / cos β
       v = c_y − f_y · tan β
   所以先由 v 求俯角、再由 u 求 γ：
       β = atan((c_y − v) / f_y)，   δ = γ = atan((u − c_x) · cos β / f_x)
       f_x, f_y 焦距、c_x, c_y 光心（像素，相機校正檔依縮放換算）
   光軸在平面圖上的方向 θ，平行線組的方向 φ = θ − δ（δ 是 φ 在光軸右邊幾度），所以
       θ = φ + δ，φ ∈ {0°, 90°, 180°, 270°}，取最接近目前航向估計的那一個。
   ui/bridge.py 把 δ 給 core/store/layout.StoreFrame.correct_heading()。
   相機假設跟車身同方向（左右沒有裝歪）、沒有側傾。

   消失點的找法：Canny → HoughLinesP → 留下斜線（跟水平夾 10°~80°，排除貨架的垂直邊與
   橫向的水平線）→ 兩兩求交點 → 取「離最多條線都很近」的交點（RANSAC 式投票）。
   至少 vp_min_lines 條線支持、而且在水平視角 ±max_angle_deg 內才採用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover — app 啟動時會檢查並報錯，這裡只是讓純函式測試能 import
    cv2 = None


@dataclass(frozen=True)
class VanishingPoint:
    u: float
    v: float
    lines: int          # 支持這個消失點的線段數


def detect_lines(gray: np.ndarray, min_len_ratio: float = 0.08) -> List[Tuple[float, float, float, float]]:
    h, w = gray.shape[:2]
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 50, 150)
    min_len = max(20, int(min_len_ratio * min(h, w)))
    raw = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=40, minLineLength=min_len, maxLineGap=8)
    # OpenCV 4 回 (N,1,4)、OpenCV 5 回 (N,4)，統一攤平
    return [] if raw is None else [tuple(map(float, l)) for l in np.asarray(raw).reshape(-1, 4)]


def _oblique(lines, lo_deg: float = 10.0, hi_deg: float = 80.0):
    out = []
    for x1, y1, x2, y2 in lines:
        a = abs(math.degrees(math.atan2(y2 - y1, x2 - x1))) % 180
        a = min(a, 180 - a)
        if lo_deg <= a <= hi_deg:
            out.append((x1, y1, x2, y2))
    return out


def _homog(l):
    x1, y1, x2, y2 = l
    v = np.cross([x1, y1, 1.0], [x2, y2, 1.0])
    return v / (math.hypot(v[0], v[1]) or 1.0)          # 正規化後 |a·x + b·y + c| = 點到線距離


def vanishing_point(lines: Sequence[Tuple[float, float, float, float]], width: int, height: int,
                    min_lines: int = 4, tol_px: Optional[float] = None) -> Optional[VanishingPoint]:
    """一組線段的消失點（見檔案開頭）。找不到可信的就回 None。"""
    obl = _oblique(lines)
    # 兩兩求交點是 O(n²)，只留最長的 60 條（Pi 5 上一張 640×360 約 20~40 ms）
    obl = sorted(obl, key=lambda l: -math.hypot(l[2] - l[0], l[3] - l[1]))[:60]
    if len(obl) < min_lines:
        return None
    tol = tol_px if tol_px is not None else 0.012 * width
    H = np.array([_homog(l) for l in obl])
    best, best_n = None, 0
    n = len(H)
    for i in range(n):
        for j in range(i + 1, n):
            p = np.cross(H[i], H[j])
            if abs(p[2]) < 1e-9:
                continue
            x, y = p[0] / p[2], p[1] / p[2]
            # 相機往下俯時，水平線的消失點在影像上方外面（v = c_y − f_y·tan β），範圍放寬
            if not (-width <= x <= 2 * width and -4 * height <= y <= 2 * height):
                continue
            d = np.abs(H @ np.array([x, y, 1.0]))
            k = int((d < tol).sum())
            if k > best_n:
                best, best_n = (x, y, d < tol), k
    if best is None or best_n < min_lines:
        return None
    # 用支持的線做最小平方，讓交點更準
    A = H[best[2]]
    sol, *_ = np.linalg.lstsq(A[:, :2], -A[:, 2], rcond=None)
    return VanishingPoint(float(sol[0]), float(sol[1]), best_n)


def vp_offset_deg(u: float, v: float, K) -> float:
    """δ = atan((u − c_x)·cos β / f_x)，β = atan((c_y − v)/f_y)，度。正值 = 消失點在光軸右邊。"""
    fx, fy, cx, cy = float(K[0][0]), float(K[1][1]), float(K[0][2]), float(K[1][2])
    beta = math.atan2(cy - v, fy)
    return math.degrees(math.atan2((u - cx) * math.cos(beta), fx))


def heading_from_vp(delta_deg: float, heading_estimate_deg: float) -> float:
    """θ = φ + δ，φ 取讓 θ 最接近目前估計的那個座標軸方向（0/90/180/270）。"""
    phi = round((heading_estimate_deg - delta_deg) / 90.0) * 90.0
    return phi + delta_deg


@dataclass(frozen=True)
class AnchorSighting:
    forward_m: float    # 標記在推車前方幾公尺
    lateral_m: float    # 標記在推車右方幾公尺（左邊為負）
    height_m: float     # 由標記算出來的相機離地高度

    @property
    def ground_m(self) -> float:
        return math.hypot(self.forward_m, self.lateral_m)


def floor_pose(rvec, tvec) -> AnchorSighting:
    """solvePnP 的結果 → 標記相對推車的水平位置（公式見檔案開頭）。"""
    R, _ = cv2.Rodrigues(np.asarray(rvec, dtype=np.float64))
    t = np.asarray(tvec, dtype=np.float64).reshape(3)
    n = R[:, 2]
    if n @ t > 0:            # 法向量要朝向相機（往上）
        n = -n
    g = t - (t @ n) * n
    z = np.array([0.0, 0.0, 1.0])
    f = z - (z @ n) * n
    f /= np.linalg.norm(f)
    r = np.cross(f, n)
    return AnchorSighting(float(g @ f), float(g @ r), float(-(n @ t)))


class AnchorDetector:
    """在灰階影像（已去畸變）裡找地上的定位點 ArUco 標記，回傳 AnchorSighting，沒看到回 None。"""

    DICT = "DICT_4X4_50"

    def __init__(self, marker_id: int, size_m: float, K):
        if cv2 is None or not hasattr(cv2, "aruco"):
            raise RuntimeError("這份 OpenCV 沒有 aruco 模組（要 opencv-contrib-python，或 Debian 的 python3-opencv）")
        self.marker_id = int(marker_id)
        self.size_m = float(size_m)
        self.K = np.asarray(K, dtype=np.float64)
        h = self.size_m / 2
        # ArUco 角點順序：左上、右上、右下、左下（標記自己的座標，z 朝外）
        self._obj = np.array([[-h, h, 0], [h, h, 0], [h, -h, 0], [-h, -h, 0]], dtype=np.float64)
        dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, self.DICT))
        if hasattr(cv2.aruco, "ArucoDetector"):                       # OpenCV ≥ 4.7
            det = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
            self._detect = det.detectMarkers
        else:                                                         # 舊版 API
            params = cv2.aruco.DetectorParameters_create()
            self._detect = lambda img: cv2.aruco.detectMarkers(img, dictionary, parameters=params)

    def detect(self, gray: np.ndarray) -> Optional[AnchorSighting]:
        corners, ids, _ = self._detect(gray)
        if ids is None:
            return None
        for c, i in zip(corners, ids.flatten()):
            if int(i) == self.marker_id:
                ok, rvec, tvec = cv2.solvePnP(self._obj, c.reshape(4, 2).astype(np.float64), self.K, None,
                                              flags=cv2.SOLVEPNP_IPPE_SQUARE)
                return floor_pose(rvec, tvec) if ok else None
        return None


def make_marker(marker_id: int, pixels: int = 600) -> np.ndarray:
    """產生定位點標記影像（四周白邊寬 = 黑框邊長的 1/6）。tools/make_anchor_marker.py 用。"""
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, AnchorDetector.DICT))
    gen = getattr(cv2.aruco, "generateImageMarker", None) or cv2.aruco.drawMarker
    img = gen(dictionary, int(marker_id), pixels)
    pad = pixels // 6
    return cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)


def scaled_intrinsics(camera_matrix, dist_coeffs, calib_size: Tuple[int, int], size: Tuple[int, int]):
    """校正時的解析度 → 處理用的解析度（縮小加速）。回傳 (K, dist)。"""
    K = np.array(camera_matrix, dtype=np.float64).copy()
    sx, sy = size[0] / calib_size[0], size[1] / calib_size[1]
    K[0, 0] *= sx
    K[0, 2] *= sx
    K[1, 1] *= sy
    K[1, 2] *= sy
    return K, np.array(dist_coeffs, dtype=np.float64)
