"""
drivers/camera_stream.py

Pi 5 相機（MIPI CSI）擷取：picamera2（libcamera）背景執行緒持續抓影格，get_latest_frame() 拿最新
一張（numpy array，直接給 OpenCV）。不用 cv2.VideoCapture：在 Pi 5 上常抓不到 CSI 相機或不穩定。
設定路徑固定用 preview_configuration + align()（跟 create_video_configuration 的 ISP 處理不一定相同，
棋盤格校正也走這條，校正結果才對得上）。

全專案只有這裡開相機（open_camera()；一顆 CSI 相機同時只能被一個 Picamera2 打開）；找貨頁的前方相機
drivers/front_camera.py 也是用 CameraStream。

接了不只一台相機（Pi 5 有兩個 CSI 埠）：config.json 的 camera.index 選哪一台當前方相機（編號是 libcamera
的順序，用 --list 看）。相機裝反了用 camera.hflip / camera.vflip 在 ISP 裡翻回來：上下顛倒（轉 180°）兩個都
true，只有左右鏡像就只開 hflip。翻轉改了影像座標，內參要跟著換（轉 180° 時主點 (c_x, c_y) 變成
(W − 1 − c_x, H − 1 − c_y)，切向畸變 p_1、p_2 變號）：同一台相機用 --flip-calibration 換算，換了相機就 --calibrate
重拍（會套用同一組翻轉）。校正檔記著量的時候的翻轉，跟 config 不一樣 drivers/front_camera.py 會報錯。

硬體測試與校正（沒給 --camera 就用 config.json 的 camera.index、hflip、vflip）：
    python3 -m drivers.camera_stream --list          # 列出接著的相機（編號、型號、接在哪個埠）
    python3 -m drivers.camera_stream --snapshot      # 每台各拍一張原始影像（不翻轉），存 data/camera_check/<時間>/
    python3 -m drivers.camera_stream --preview       # 前方相機擷取 10 張，印 shape
    python3 -m drivers.camera_stream --color-test    # 前方相機拍一張存 PNG，肉眼確認色彩通道順序
    python3 -m drivers.camera_stream --flip-calibration
        # 同一台相機只改了翻轉：現有內參直接換算成 config 的翻轉（不用重拍棋盤格），原檔備份
    python3 -m drivers.camera_stream --calibrate --square-size-mm 25.8
        # 前方相機棋盤格內參校正（照片存 data/camera_calibration/<時間>/）
    python3 -m drivers.camera_stream --calibrate --from-data data/camera_calibration/<時間> [--dry-run]
        # 用存下來的照片重算（不開相機）（預設 9x6 內角點），存成 config.json 的 camera.calibration_file
    任何一項加 --camera N 指定別台（這時不套 config 的翻轉，除非也加 --hflip / --vflip）
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

# libcamera 的 C++ 記錄預設印 INFO／WARN（imx708_wide 沒有感測器屬性資料庫、USB 攝影機第一張序號不是 0 等），
# 都無害；只留錯誤。要看全部：執行前 export LIBCAMERA_LOG_LEVELS=*:INFO（已經設了就不蓋掉）。
os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:ERROR")
logging.getLogger("picamera2").setLevel(logging.WARNING)

try:
    from picamera2 import Picamera2
except ImportError:  # pragma: no cover - 開發機（非 Pi）上通常裝不了 picamera2
    Picamera2 = None

try:
    from libcamera import Transform
except ImportError:  # pragma: no cover
    Transform = None

logger = logging.getLogger(__name__)


def camera_list() -> list:
    """接著的相機（libcamera 的順序）：[{"Num", "Model", "Location", "Rotation", "Id"}, ...]。"""
    if Picamera2 is None:
        raise RuntimeError("picamera2 未安裝或無法在此平台使用。在 Pi 5 上請用：sudo apt install -y python3-picamera2")
    return list(Picamera2.global_camera_info())


def full_fov_mode(modes: list, resolution, framerate: Optional[float] = None) -> Optional[dict]:
    """從感測器模式裡挑**完整視野**（crop_limits 涵蓋整個感光區）、輸出至少 resolution、幀率夠的最小那個。

    不指定的話 libcamera 會挑「解析度最接近」的模式，imx708 對 1280x720 會挑 1536x864——那是**裁切中間**的
    模式，廣角鏡的視野被砍掉一大塊（11-26 實測比 rpicam-hello 小很多）。完整視野的模式（imx708 是 2304x1296
    2x2 合併）輸出再由 ISP 縮到 resolution，視野跟 rpicam-hello 一樣。
    """
    def area(m):
        c = m.get("crop_limits") or (0, 0, 0, 0)
        return c[2] * c[3]

    if not modes:
        return None
    full = max(area(m) for m in modes)
    cands = [m for m in modes if area(m) >= 0.98 * full]
    big = [m for m in cands if m["size"][0] >= resolution[0] and m["size"][1] >= resolution[1]
           and (not framerate or float(m.get("fps", 0)) >= float(framerate))]
    pool = big or cands
    return min(pool, key=lambda m: m["size"][0] * m["size"][1]) if big else max(pool, key=lambda m: m["size"][0] * m["size"][1])


def open_camera(resolution=(1280, 720), pixel_format: str = "RGB888", camera_num: int = 0,
                hflip: bool = False, vflip: bool = False, framerate: Optional[int] = None) -> "Picamera2":
    """打開並啟動一台相機，全專案唯一的設定路徑。

    preview_configuration（不是 create_video_configuration）+ align()：兩條 stream role 的 ISP 處理不一定一樣
    （實測 create_video_configuration 顏色會錯），align() 讓寬高符合感測器的 stride 要求、避免畫面錯位。
    翻轉在 ISP 裡做（libcamera Transform），之後拿到的影格已經是翻好的。
    CSI 相機固定用完整視野的感測器模式（full_fov_mode），選到的模式記在 picam2.sensor_size_used，
    校正檔會記下來、drivers/front_camera.py 核對（模式不同內參就不同）。USB 攝影機沒有感測器模式，是 None。
    """
    if Picamera2 is None:
        raise RuntimeError("picamera2 未安裝或無法在此平台使用。在 Pi 5 上請用：sudo apt install -y python3-picamera2")
    cams = camera_list()
    if not 0 <= camera_num < len(cams):
        raise RuntimeError(f"沒有編號 {camera_num} 的相機：目前接著 {len(cams)} 台（python3 -m drivers.camera_stream --list）")
    picam2 = Picamera2(camera_num)
    try:
        cfg = picam2.preview_configuration
        cfg.main.size = tuple(resolution)
        cfg.main.format = pixel_format
        if hflip or vflip:
            if Transform is None:
                raise RuntimeError("找不到 libcamera 的 Transform，沒辦法翻轉影像")
            cfg.transform = Transform(hflip=int(bool(hflip)), vflip=int(bool(vflip)))
        try:
            modes = list(picam2.sensor_modes)
        except Exception:      # USB 攝影機（uvcvideo）沒有感測器模式
            modes = []
        mode = full_fov_mode(modes, resolution, framerate)
        if mode is not None:
            if hasattr(cfg, "sensor"):
                cfg.sensor.output_size = tuple(mode["size"])
                cfg.sensor.bit_depth = mode["bit_depth"]
            else:              # 舊版 picamera2：用 raw 串流的大小指定模式
                cfg.enable_raw()
                cfg.raw.size = tuple(mode["size"])
        cfg.align()
        picam2.configure("preview")
        used = picam2.camera_configuration().get("sensor") or {}
        size = used.get("output_size") or (picam2.camera_configuration().get("raw") or {}).get("size")
        picam2.sensor_size_used = tuple(size) if (mode is not None and size) else None
        if mode is not None and picam2.sensor_size_used != tuple(mode["size"]):
            raise RuntimeError(f"要求感測器模式 {tuple(mode['size'])}，實際是 {picam2.sensor_size_used}（視野會不一樣）")
        if framerate:
            picam2.set_controls({"FrameRate": framerate})
        picam2.start()
    except Exception:
        picam2.close()
        raise
    return picam2


def frame_to_bgr(picam2: "Picamera2", frame: np.ndarray) -> np.ndarray:
    """統一成 OpenCV 的 BGR。

    CSI 相機（RGB888）：picamera2 給的記憶體順序本來就是 B, G, R，不動。
    USB 攝影機（uvcvideo）不支援 RGB888，給 MJPEG：picamera2 已經解碼的是 R, G, B 順序（直接存檔紅藍對調，
    11-26 實測 C270 就是這樣），要轉；還沒解碼（一維的 JPEG 資料）就自己解。
    """
    fmt = str(picam2.camera_configuration()["main"]["format"])
    if fmt != "MJPEG":
        return frame
    if frame.ndim == 1:
        img = cv2.imdecode(frame, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"MJPEG 影格解不開（{frame.size} bytes）")
        return img
    return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)


def flip_calibration(camera_matrix: np.ndarray, dist_coeffs: np.ndarray, size: tuple[int, int],
                     hflip: bool, vflip: bool) -> tuple[np.ndarray, np.ndarray]:
    """同一台相機、影像多翻轉一次（hflip 左右、vflip 上下）之後的內參。

    寬 W、高 H（像素）。左右翻：u → W − 1 − u，正規化座標 x → −x；上下翻：v → H − 1 − v，y → −y。
    代進畸變模型 x_d = x(1 + k₁r² + k₂r⁴ + k₃r⁶) + 2p₁xy + p₂(r² + 2x²)、
    y_d = y(1 + …) + p₁(r² + 2y²) + 2p₂xy：徑向項 k 不變，左右翻 p₂ 變號、上下翻 p₁ 變號，
    主點 c_x → W − 1 − c_x、c_y → H − 1 − c_y，焦距不變。
    """
    K = np.array(camera_matrix, dtype=float).copy()
    d = np.array(dist_coeffs, dtype=float).reshape(-1).copy()
    W, H = size
    if hflip:
        K[0, 2] = W - 1 - K[0, 2]
        if d.size > 3:
            d[3] = -d[3]
    if vflip:
        K[1, 2] = H - 1 - K[1, 2]
        if d.size > 2:
            d[2] = -d[2]
    return K, d.reshape(np.shape(dist_coeffs))


def calibration_flip(data) -> tuple[bool, bool]:
    """校正檔是在哪一組翻轉下量的（舊檔沒記，當成沒翻）。"""
    if "hflip" in data and "vflip" in data:
        return bool(data["hflip"]), bool(data["vflip"])
    return False, False


class CameraStream:
    """用 picamera2 在背景執行緒持續擷取影格，主執行緒用 get_latest_frame() 取最新一張。

    畸變校正是選用的：如果提供 calibration_file 且檔案存在，每一影格會先套用
    cv2.undistort() 再存進 buffer；給了 calibration_file 但檔案不在，輸出原始影格並記一次警告。
    calibration_file=None 是刻意要原始影格（例如 drivers/front_camera.py 縮小後自己去畸變），不警告。

    色彩通道：picamera2 的 RGB888 記憶體順序是 B, G, R，可以直接給 cv2（COLOR_BGR2GRAY 等）。
    目前只用灰階，順序不影響結果；要用顏色時先用 --color-test 拍一張確認。
    """

    def __init__(
        self,
        resolution: tuple[int, int] = (1280, 720),
        framerate: int = 30,
        calibration_file: Optional[str | Path] = None,
        pixel_format: str = "RGB888",
        camera_num: int = 0,
        hflip: bool = False,
        vflip: bool = False,
    ):
        if Picamera2 is None:
            raise RuntimeError(
                "picamera2 未安裝或無法在此平台使用。"
                "在 Pi 5 上請用：sudo apt install -y python3-picamera2"
            )
        if cv2 is None:
            raise RuntimeError("opencv-python 未安裝，請 pip install opencv-python")

        self.resolution = resolution
        self.framerate = framerate
        self.pixel_format = pixel_format
        self.camera_num, self.hflip, self.vflip = int(camera_num), bool(hflip), bool(vflip)
        self.sensor_size_used: Optional[tuple] = None

        self._camera_matrix: Optional[np.ndarray] = None
        self._dist_coeffs: Optional[np.ndarray] = None
        self._undistort_warned = False
        self._calibration_requested = bool(calibration_file)
        if calibration_file:
            self._load_calibration(Path(calibration_file))

        self._picam2: Optional["Picamera2"] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_time: Optional[float] = None   # 最新一張影格擷取完成的 time.monotonic()
        self._frame_ready = threading.Event()

    # ------------------------------------------------------------------
    def _load_calibration(self, path: Path) -> None:
        if not path.exists():
            logger.warning(
                "找不到相機校正檔 %s，會先用未校正的原始影格。"
                "請執行 `python3 -m drivers.camera_stream --calibrate` 產生。",
                path,
            )
            return
        data = np.load(str(path))
        self._camera_matrix = data["camera_matrix"]
        self._dist_coeffs = data["dist_coeffs"]
        logger.info("已載入相機校正檔：%s", path)

    def _undistort(self, frame: np.ndarray) -> np.ndarray:
        if self._camera_matrix is None or self._dist_coeffs is None:
            if self._calibration_requested and not self._undistort_warned:
                logger.warning("尚未載入相機內參，輸出未校正影格（影響消失點/視覺定位精度）")
                self._undistort_warned = True
            return frame
        return cv2.undistort(frame, self._camera_matrix, self._dist_coeffs)

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("CameraStream 已經在執行中")

        self._picam2 = open_camera(self.resolution, self.pixel_format, self.camera_num,
                                   self.hflip, self.vflip, framerate=self.framerate)
        self.sensor_size_used = self._picam2.sensor_size_used
        # 讓 AE/AWB 有時間收斂，避免前幾張影格過曝/偏色
        time.sleep(0.5)

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="CameraStreamThread", daemon=True)
        self._thread.start()
        logger.info("相機串流背景執行緒已啟動（相機 %d，%dx%d @ %dfps，hflip=%s vflip=%s）",
                    self.camera_num, *self.resolution, self.framerate, self.hflip, self.vflip)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._picam2 is not None:
            self._picam2.stop()
            self._picam2.close()
            self._picam2 = None
        logger.info("相機串流背景執行緒已停止")

    def _run_loop(self) -> None:
        assert self._picam2 is not None
        while not self._stop_event.is_set():
            frame = frame_to_bgr(self._picam2, self._picam2.capture_array())
            frame = self._undistort(frame)
            with self._lock:
                self._latest_frame = frame
                self._latest_time = time.monotonic()
            self._frame_ready.set()

    def frame_age(self) -> Optional[float]:
        """最新一張影格距今幾秒；還沒拿到任何影格回 None。

        get_latest_frame() 只要拿過一張就會一直回傳東西——背景執行緒如果因為相機
        斷線而結束，回傳的會是同一張舊影格。要確認影像還是活的，看這個值。
        """
        with self._lock:
            return None if self._latest_time is None else time.monotonic() - self._latest_time

    def get_latest_frame(self, timeout: Optional[float] = 1.0) -> Optional[np.ndarray]:
        """取得目前最新的一張影格（已套用畸變校正，如果有校正檔的話）。"""
        if not self._frame_ready.wait(timeout=timeout):
            return None
        with self._lock:
            return None if self._latest_frame is None else self._latest_frame.copy()


# ----------------------------------------------------------------------
# 棋盤格校正工具
# ----------------------------------------------------------------------
def _subpix_window(corners: np.ndarray, board_size: tuple[int, int]) -> int:
    """角點精修的半窗寬：相鄰角點間距的 0.3 倍（3~11 px）。

    固定 11（窗口 23×23 px）在廣角鏡下會比一格還大（棋盤 50 cm 外一格只有二十幾個像素），窗口蓋到隔壁的角點，
    精修被拉偏，重投影誤差降不下來。
    """
    grid = np.asarray(corners).reshape(board_size[1], board_size[0], 2)
    gaps = np.concatenate([np.linalg.norm(np.diff(grid, axis=1), axis=2).ravel(),
                           np.linalg.norm(np.diff(grid, axis=0), axis=2).ravel()])
    return int(np.clip(0.3 * float(np.min(gaps)), 3, 11))


def _find_corners(gray: np.ndarray, board_size: tuple[int, int]) -> Optional[np.ndarray]:
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    found, corners = cv2.findChessboardCorners(gray, board_size, flags)
    if not found:
        return None
    w = _subpix_window(corners, board_size)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 0.001)
    return cv2.cornerSubPix(gray, corners, (w, w), (-1, -1), criteria)


def compute_calibration(grays: list, board_size: tuple[int, int], square_size_mm: float) -> dict:
    """一組棋盤格照片 → 內參。每張照片各算重投影誤差，比中位數大很多的（> max(3 × 中位數, 中位數 + 0.5 px)）
    剔除後重算一次。回傳 K、dist、rms、每張的誤差與有沒有用上。"""
    objp = np.zeros((board_size[0] * board_size[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:board_size[0], 0:board_size[1]].T.reshape(-1, 2)
    objp *= square_size_mm
    views = []
    for i, g in enumerate(grays):
        c = _find_corners(g, board_size)
        if c is not None:
            views.append((i, c))
    if len(views) < 8:
        raise RuntimeError(f"找到棋盤格的照片只有 {len(views)} 張，至少要 8 張")
    shape = grays[0].shape[::-1]
    flags = cv2.CALIB_RATIONAL_MODEL

    def calib(vs):
        rms, K, d, rv, tv = cv2.calibrateCamera([objp] * len(vs), [c for _, c in vs], shape, None, None, flags=flags)
        errs = []
        for (_, c), r, t in zip(vs, rv, tv):
            proj, _ = cv2.projectPoints(objp, r, t, K, d)
            errs.append(float(np.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - c.reshape(-1, 2)) ** 2, axis=1)))))
        return rms, K, d, errs

    rms0, K, d, errs = calib(views)
    med = float(np.median(errs))
    keep = [v for v, e in zip(views, errs) if e <= max(3 * med, med + 0.5)]
    kept_ids = {i for i, _ in keep}
    dropped = [i for i, _ in views if i not in kept_ids]
    if dropped and len(keep) >= 8:
        rms, K, d, _ = calib(keep)
    else:
        rms, keep, dropped = rms0, views, []
    per_view = {i: e for (i, _), e in zip(views, errs)}
    return {"camera_matrix": K, "dist_coeffs": d, "rms": float(rms), "rms_all": float(rms0),
            "per_view": per_view, "used": [i for i, _ in keep], "dropped": dropped,
            "not_found": [i for i in range(len(grays)) if i not in per_view]}


def run_chessboard_calibration(
    output_path: Path,
    board_size: tuple[int, int] = (9, 6),
    square_size_mm: float = 25.0,
    num_images: int = 25,
    resolution: tuple[int, int] = (1280, 720),
    show_preview: bool = True,
    camera_num: int = 0,
    hflip: bool = False,
    vflip: bool = False,
    from_data: Optional[Path] = None,
    write: bool = True,
) -> None:
    """棋盤格內參校正。

    擷取（沒給 from_data）：預覽畫面上找到棋盤格、而且**連續兩張影格角點幾乎沒動**（最大位移 < 0.7 px）才拍，
    手晃造成的模糊不會進來；兩張之間至少隔 1 秒、角點要移動過（換個位置）。每張拍到的灰階照片連同設定存在
    data/camera_calibration/<時間>/，之後可以 from_data 重算，不用重拍。
    計算見 compute_calibration：有理畸變模型（廣角鏡邊緣的畸變 5 個係數壓不住）、剔除誤差特別大的照片。
    """
    import json
    from datetime import datetime

    from core import config

    if cv2 is None:
        raise RuntimeError("需要 opencv-python 才能執行校正")

    if from_data is not None:
        meta = json.loads((from_data / "meta.json").read_text(encoding="utf-8"))
        files = sorted(from_data.glob("img_*.png"))
        grays = [cv2.imread(str(f), cv2.IMREAD_GRAYSCALE) for f in files]
        board_size = tuple(meta["board_size"])
        square_size_mm = float(meta["square_size_mm"])
        resolution = tuple(meta["resolution"])
        hflip, vflip = bool(meta["hflip"]), bool(meta["vflip"])
        sensor_size = tuple(meta["sensor_size"]) if meta.get("sensor_size") else None
        print(f"讀入 {config.rel(from_data)}：{len(grays)} 張，棋盤 {board_size[0]}x{board_size[1]} 內角點、"
              f"一格 {square_size_mm} mm")
    else:
        grays, sensor_size = _capture_chessboard(board_size, num_images, resolution, show_preview,
                                                 camera_num, hflip, vflip)
        out_dir = config.DATA_DIR / "camera_calibration" / datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)
        for k, g in enumerate(grays):
            cv2.imwrite(str(out_dir / f"img_{k:02d}.png"), g)
        (out_dir / "meta.json").write_text(json.dumps({
            "board_size": list(board_size), "square_size_mm": square_size_mm, "resolution": list(resolution),
            "camera_num": camera_num, "hflip": hflip, "vflip": vflip,
            "sensor_size": list(sensor_size) if sensor_size else None,
            "created_at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"照片存在 {config.rel(out_dir)}（之後可以 --from-data 重算）")

    if len(grays) < 8:
        raise RuntimeError(f"只拍到 {len(grays)} 張，至少要 8 張")
    r = compute_calibration(grays, board_size, square_size_mm)
    print("\n每張照片的重投影誤差（px）：")
    for i in sorted(r["per_view"]):
        mark = "  ← 剔除" if i in r["dropped"] else ""
        print(f"  img_{i:02d}  {r['per_view'][i]:.3f}{mark}")
    for i in r["not_found"]:
        print(f"  img_{i:02d}  （重新偵測沒找到棋盤格，沒用）")
    print(f"\n全部 {len(r['per_view'])} 張：reprojection error = {r['rms_all']:.4f}")
    if r["dropped"]:
        print(f"剔除 {len(r['dropped'])} 張後 {len(r['used'])} 張：reprojection error = {r['rms']:.4f}")
    print("（越接近 0 越好；廣角鏡 1280x720 一般 0.3~0.8，超過 1 先看是哪幾張特別大、是不是模糊或棋盤不平）")
    K = r["camera_matrix"]
    print(f"f_x = {K[0, 0]:.1f}、f_y = {K[1, 1]:.1f}、c_x = {K[0, 2]:.1f}、c_y = {K[1, 2]:.1f}（px）")

    if not write:
        print("（--dry-run：沒有寫入校正檔）")
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # 記下量的時候的翻轉與感測器模式：drivers/front_camera.py 會核對，對不上就報錯
    np.savez(str(output_path), camera_matrix=K, dist_coeffs=r["dist_coeffs"],
             hflip=bool(hflip), vflip=bool(vflip), size=np.array(resolution),
             sensor_size=np.array(sensor_size if sensor_size else (0, 0)), rms=r["rms"])
    print(f"已存到 {output_path}")


def _capture_chessboard(board_size, num_images, resolution, show_preview, camera_num, hflip, vflip):
    """開相機拍棋盤格，回傳 (灰階照片 list, 感測器模式)。"""
    picam2 = open_camera(resolution, "RGB888", camera_num, hflip, vflip)
    sensor_size = picam2.sensor_size_used
    time.sleep(0.5)
    print(f"相機 {camera_num}（hflip={hflip}、vflip={vflip}，感測器模式 {sensor_size}）")

    window_name = "SmartCart calibration (q = stop)"
    preview_active = show_preview
    if preview_active:
        try:
            cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        except cv2.error as exc:
            print(f"[警告] 無法開啟預覽視窗（{exc}），改用無畫面模式繼續校正。")
            preview_active = False
    print(f"目標 {num_images} 張：棋盤格移到畫面中間、四個角落、四個邊，再加幾張傾斜的；"
          "看到綠色角點後**拿穩不動**，停住才會拍。")

    grays: list = []
    prev = None
    last_shot_t, last_shot_c = 0.0, None
    try:
        while len(grays) < num_images:
            frame = frame_to_bgr(picam2, picam2.capture_array())
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            found, corners = cv2.findChessboardCorners(
                gray, board_size, cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_FAST_CHECK)
            status, color = "not found - adjust angle/distance", (0, 0, 220)
            if found:
                pts = corners.reshape(-1, 2)      # OpenCV 版本不同，回傳 (N, 1, 2) 或 (N, 2)
                move = None if prev is None else float(np.max(np.linalg.norm(pts - prev, axis=1)))
                moved_since_shot = (last_shot_c is None
                                    or float(np.median(np.linalg.norm(pts - last_shot_c, axis=1))) > 30)
                if move is None or move >= 0.7:
                    status, color = "found - hold still", (0, 200, 255)
                elif not moved_since_shot:
                    status, color = "same spot - move to another place", (0, 200, 255)
                elif time.time() - last_shot_t < 1.0:
                    status, color = "wait", (0, 200, 255)
                else:
                    grays.append(gray.copy())
                    last_shot_t, last_shot_c = time.time(), pts.copy()
                    status, color = "CAPTURED", (0, 220, 0)
                    print(f"  已擷取 {len(grays)}/{num_images}")
                prev = pts
            else:
                prev = None
            if preview_active:
                disp = frame.copy()
                if found:
                    cv2.drawChessboardCorners(disp, board_size, corners, found)
                cv2.rectangle(disp, (0, 0), (disp.shape[1], 70), (0, 0, 0), -1)
                cv2.putText(disp, status, (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2, cv2.LINE_AA)
                cv2.putText(disp, f"captured {len(grays)}/{num_images}", (15, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                            (255, 255, 255), 2, cv2.LINE_AA)
                cv2.imshow(window_name, disp)
                if (cv2.waitKey(1) & 0xFF) == ord("q"):
                    print("使用者按 q，提前結束擷取。")
                    break
    except KeyboardInterrupt:
        print("提前結束擷取。")
    finally:
        picam2.stop()
        picam2.close()
        if preview_active:
            cv2.destroyWindow(window_name)
    return grays, sensor_size


def run_color_test(output_path: Path, resolution: tuple[int, int] = (1280, 720),
                   camera_num: int = 0, hflip: bool = False, vflip: bool = False) -> None:
    """拍一張照片、原封不動存成 PNG，用來明確驗證色彩通道順序到底對不對。

    不做任何 RGB<->BGR 轉換——直接把 picamera2.capture_array() 回傳的 array
    丟給 cv2.imwrite()。cv2.imwrite() 的慣例是「輸入陣列視為 BGR」，如果存出來
    的 PNG 用一般看圖軟體打開，顏色是對的（藍色的東西看起來就是藍色），代表
    picamera2 這個設定路徑（preview_configuration + align()）給的原始資料確實
    已經是 BGR 排列，程式碼裡不需要額外轉換（目前的假設）。如果顏色是對調的
    （藍色變橘紅色、紅色變藍綠色），代表原始資料其實是 RGB 排列，程式碼裡
    所有用到這個 array 的地方（get_latest_frame()、預覽視窗、灰階轉換）都需要
    先加一次 cv2.cvtColor(frame, cv2.COLOR_RGB2BGR) 再用。

    建議測試時鏡頭前放一個顏色明確、辨識度高的東西（例如純紅色或純藍色的物品），
    不要用膚色/木頭色這種曖昧的顏色，肉眼比對才會準。
    """
    if Picamera2 is None or cv2 is None:
        raise RuntimeError("需要 picamera2 與 opencv-python 才能執行色彩驗證")

    picam2 = open_camera(resolution, "RGB888", camera_num, hflip, vflip)
    time.sleep(1.0)  # 讓 AE/AWB 收斂

    try:
        frame = frame_to_bgr(picam2, picam2.capture_array())
    finally:
        picam2.stop()
        picam2.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # CSI 相機不做任何顏色轉換（frame_to_bgr 原樣傳回）：這張圖存出來的樣子，就是「假設不用轉換」
    # 這個前提的直接驗證結果。USB 攝影機（MJPEG）會轉成 BGR。
    cv2.imwrite(str(output_path), frame)
    print(f"已存到 {output_path}（沒有做任何顏色轉換，原封不動存的）")
    print("請把這張圖傳回來給我看，或自己打開來對照鏡頭前物品的實際顏色：")
    print("  - 顏色正常（例如藍色的東西看起來是藍色）→ 目前程式碼不用改")
    print("  - 顏色對調（例如藍色的東西看起來是橘紅色）→ 需要加一次 RGB<->BGR 轉換")


def run_list() -> None:
    cams = camera_list()
    if not cams:
        raise RuntimeError("libcamera 找不到任何相機：檢查排線方向、兩頭有沒有扣緊，`rpicam-hello --list-cameras` 看系統認不認得")
    for c in cams:
        print(f"相機 {c.get('Num')}：{c.get('Model')}　Location={c.get('Location')}　Rotation={c.get('Rotation')}　{c.get('Id')}")


def run_snapshot(out_dir: Path, resolution: tuple[int, int], cameras: Optional[list] = None) -> list:
    """每台相機各拍一張**原始**影像（不翻轉），左上角標相機編號。看畫面判斷哪台是前方相機、要不要翻。"""
    cams = camera_list()
    if not cams:
        raise RuntimeError("libcamera 找不到任何相機")
    nums = cameras if cameras is not None else [int(c.get("Num", i)) for i, c in enumerate(cams)]
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for n in nums:
        picam2 = open_camera(resolution, "RGB888", n)
        try:
            time.sleep(1.0)   # 讓 AE/AWB 收斂
            img = frame_to_bgr(picam2, picam2.capture_array()).copy()
        finally:
            picam2.stop()
            picam2.close()
        label = f"cam {n}  {cams[n].get('Model')}  (raw, no flip)"
        cv2.rectangle(img, (0, 0), (min(img.shape[1], 30 + 22 * len(label)), 60), (0, 0, 0), -1)
        cv2.putText(img, label, (15, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2, cv2.LINE_AA)
        path = out_dir / f"cam{n}.png"
        cv2.imwrite(str(path), img)
        paths.append(path)
        print(f"相機 {n}：{path}")
    return paths


def run_flip_calibration(path: Path, resolution: tuple[int, int], hflip: bool, vflip: bool) -> None:
    """把校正檔換算到目標翻轉（hflip, vflip）。原檔備份成 <名稱>.bak-<時間>.npz。"""
    from datetime import datetime

    if not path.exists():
        raise FileNotFoundError(f"找不到校正檔 {path}")
    data = dict(np.load(str(path)))
    h0, v0 = calibration_flip(data)
    dh, dv = h0 != hflip, v0 != vflip
    if not (dh or dv):
        print(f"{path} 已經是 hflip={hflip}、vflip={vflip} 量的，不用換算")
        return
    K, d = flip_calibration(data["camera_matrix"], data["dist_coeffs"], resolution, dh, dv)
    backup = path.with_name(f"{path.stem}.bak-{datetime.now():%Y%m%d-%H%M%S}.npz")
    np.savez(str(backup), **data)
    data.update(camera_matrix=K, dist_coeffs=d, hflip=bool(hflip), vflip=bool(vflip), size=np.array(resolution))
    np.savez(str(path), **data)
    k0 = np.asarray(np.load(str(backup))["camera_matrix"])
    print(f"翻轉 hflip {h0}→{hflip}、vflip {v0}→{vflip}（影像 {resolution[0]}x{resolution[1]}）")
    print(f"  主點 c_x {k0[0, 2]:.1f} → {K[0, 2]:.1f}、c_y {k0[1, 2]:.1f} → {K[1, 2]:.1f}；焦距不變")
    print(f"  已寫入 {path}，原檔備份 {backup}")


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from datetime import datetime

    from core import config

    cam_cfg = config.section("camera")
    parser = argparse.ArgumentParser(description="相機串流 / 校正工具")
    parser.add_argument("--list", action="store_true", help="列出接著的相機")
    parser.add_argument("--snapshot", action="store_true", help="每台相機各拍一張原始影像（不翻轉）")
    parser.add_argument("--calibrate", action="store_true", help="執行棋盤格校正流程")
    parser.add_argument("--preview", action="store_true", help="單純測試擷取影格並印出 shape")
    parser.add_argument("--flip-calibration", action="store_true",
                        help="同一台相機改了翻轉：把現有內參換算成 config 的 camera.hflip／vflip（不用重拍棋盤格）")
    parser.add_argument("--color-test", action="store_true",
                        help="拍一張照片原封不動存成 PNG，用來驗證色彩通道順序（RGB vs BGR）")
    parser.add_argument("--camera", type=int, default=None,
                        help="相機編號（預設 config.json 的 camera.index；指定時不套 config 的翻轉）")
    parser.add_argument("--hflip", action="store_true", help="左右翻轉（跟 --camera 一起用）")
    parser.add_argument("--vflip", action="store_true", help="上下翻轉（跟 --camera 一起用）")
    parser.add_argument("--no-window", action="store_true",
                        help="校正時不開預覽視窗（沒有 X11 畫面可用時加這個，例如純文字 SSH）")
    parser.add_argument("--color-test-output", default="color_test.png",
                        help="--color-test 的圖片存到哪（--output 是校正結果 .npz，兩個不一樣）")
    parser.add_argument("--num-images", type=int, default=25, help="校正拍幾張（廣角鏡要多拍邊角，預設 25）")
    parser.add_argument("--board-cols", type=int, default=9, help="棋盤格內角點欄數")
    parser.add_argument("--board-rows", type=int, default=6, help="棋盤格內角點列數")
    parser.add_argument("--square-size-mm", type=float, default=25.0)
    parser.add_argument("--from-data", default=None, help="用之前拍的棋盤格照片重算（data/camera_calibration/<時間>）")
    parser.add_argument("--dry-run", action="store_true", help="校正只算不寫入校正檔")
    parser.add_argument("--output", default=None, help="校正結果輸出路徑（預設 config.json 的 camera.calibration_file）")
    args = parser.parse_args()

    resolution = tuple(cam_cfg.get("resolution", [1280, 720]))
    if args.camera is None:
        num, hflip, vflip = int(cam_cfg.get("index", 0)), bool(cam_cfg.get("hflip")), bool(cam_cfg.get("vflip"))
    else:
        num, hflip, vflip = args.camera, args.hflip, args.vflip
    output = Path(args.output) if args.output else config.ROOT / cam_cfg.get("calibration_file", "vision/camera_calibration.npz")

    if args.list:
        run_list()
        return

    if args.snapshot:
        out_dir = config.DATA_DIR / "camera_check" / datetime.now().strftime("%Y%m%d-%H%M%S")
        run_snapshot(out_dir, resolution, None if args.camera is None else [args.camera])
        return

    if args.flip_calibration:
        run_flip_calibration(output, resolution, hflip, vflip)
        return

    if args.calibrate:
        run_chessboard_calibration(
            output_path=output,
            board_size=(args.board_cols, args.board_rows),
            square_size_mm=args.square_size_mm,
            num_images=args.num_images,
            resolution=resolution,
            show_preview=not args.no_window,
            camera_num=num, hflip=hflip, vflip=vflip,
            from_data=Path(args.from_data) if args.from_data else None,
            write=not args.dry_run,
        )
        return

    if args.color_test:
        run_color_test(output_path=Path(args.color_test_output), resolution=resolution,
                       camera_num=num, hflip=hflip, vflip=vflip)
        return

    if args.preview:
        stream = CameraStream(resolution=resolution, calibration_file=output, camera_num=num, hflip=hflip, vflip=vflip)
        stream.start()
        try:
            for _ in range(10):
                frame = stream.get_latest_frame(timeout=2.0)
                if frame is None:
                    print("未取得影格（timeout）")
                else:
                    print(f"frame shape={frame.shape} dtype={frame.dtype}")
                time.sleep(0.3)
        finally:
            stream.stop()
        return

    parser.print_help()


if __name__ == "__main__":
    _main()
