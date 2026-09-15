"""
drivers/camera_stream.py

Pi 5 官方廣角相機（MIPI CSI）影像擷取層。

Pi 5 的相機軟體堆疊是 libcamera-based；單純用 cv2.VideoCapture() 常常抓不到
CSI 相機，或行為不穩定（V4L2 相容層在不同版本表現不一致）。這裡改用官方
picamera2 函式庫取得影格，回傳的是 numpy array，可以直接餵給 OpenCV 用。

本檔案提供兩個功能：
    1. CameraStream：背景執行緒持續擷取影格，並可套用棋盤格內參校正結果做
       即時畸變校正（cv2.undistort），供 vision/ 各模組使用。
    2. 棋盤格校正工具（--calibrate）：現場對著棋盤格拍多張照片，計算相機
       內參矩陣與畸變係數，存成 config.json 裡 camera.calibration_file 指到
       的 .npz 檔。

用法：
    # 即時預覽 + 存校正檔（需要在鏡頭前放印出來的棋盤格，預設 9x6 內角點）
    python3 -m drivers.camera_stream --calibrate --num-images 15

    # 單純測試擷取（不需要棋盤格）
    python3 -m drivers.camera_stream --preview
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

try:
    from picamera2 import Picamera2
except ImportError:  # pragma: no cover - 開發機（非 Pi）上通常裝不了 picamera2
    Picamera2 = None

logger = logging.getLogger(__name__)


class CameraStream:
    """用 picamera2 在背景執行緒持續擷取影格，主執行緒用 get_latest_frame() 取最新一張。

    畸變校正是選用的：如果提供 calibration_file 且檔案存在，每一影格會先套用
    cv2.undistort() 再存進 buffer；沒有校正檔就直接輸出原始影格（並記一次警告）。

    注意色彩通道順序：目前程式碼假設 get_latest_frame() 回傳的 numpy array可以
    直接餵給 cv2 系列函式（cv2.imshow、cv2.cvtColor(..., COLOR_BGR2GRAY) 等）用，
    不需要額外做 RGB<->BGR 轉換。這個假設**還沒有在實體相機上做過明確驗證**——
    用 `python3 -m drivers.camera_stream --color-test` 拍一張已知顏色的東西存成
    PNG，肉眼比對顏色對不對，可以一次性確認清楚。如果顏色對調（比如藍色的東西
    存出來變橘紅色），代表這裡的假設是錯的，需要在 get_latest_frame() 回傳前加
    一次 cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)。Phase 3 之後 vision/ 底下的模組
    如果要用顏色資訊，記得先確認過這件事再依賴這個假設。
    """

    def __init__(
        self,
        resolution: tuple[int, int] = (1280, 720),
        framerate: int = 30,
        calibration_file: Optional[str | Path] = None,
        pixel_format: str = "RGB888",
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

        self._camera_matrix: Optional[np.ndarray] = None
        self._dist_coeffs: Optional[np.ndarray] = None
        self._undistort_warned = False
        if calibration_file:
            self._load_calibration(Path(calibration_file))

        self._picam2: Optional["Picamera2"] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
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
            if not self._undistort_warned:
                logger.warning("尚未載入相機內參，輸出未校正影格（影響消失點/視覺定位精度）")
                self._undistort_warned = True
            return frame
        return cv2.undistort(frame, self._camera_matrix, self._dist_coeffs)

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("CameraStream 已經在執行中")

        self._picam2 = Picamera2()
        # 用 preview_configuration（不是 create_video_configuration）+ align()。
        # 這跟 create_video_configuration 是 picamera2 兩條不同的 stream role
        # 設定路徑，底層 ISP 處理不一定一樣；align() 則是確保寬高有依照感測器
        # 要求的 stride 對齊，避免緩衝區沒對齊造成的畫面錯位/花屏。
        self._picam2.preview_configuration.main.size = self.resolution
        self._picam2.preview_configuration.main.format = self.pixel_format
        self._picam2.preview_configuration.align()
        self._picam2.configure("preview")
        self._picam2.set_controls({"FrameRate": self.framerate})
        self._picam2.start()
        # 讓 AE/AWB 有時間收斂，避免前幾張影格過曝/偏色
        time.sleep(0.5)

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="CameraStreamThread", daemon=True)
        self._thread.start()
        logger.info("相機串流背景執行緒已啟動 (%dx%d @ %dfps)", *self.resolution, self.framerate)

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
            frame = self._picam2.capture_array()
            frame = self._undistort(frame)
            with self._lock:
                self._latest_frame = frame
            self._frame_ready.set()

    def get_latest_frame(self, timeout: Optional[float] = 1.0) -> Optional[np.ndarray]:
        """取得目前最新的一張影格（已套用畸變校正，如果有校正檔的話）。"""
        if not self._frame_ready.wait(timeout=timeout):
            return None
        with self._lock:
            return None if self._latest_frame is None else self._latest_frame.copy()


# ----------------------------------------------------------------------
# 棋盤格校正工具
# ----------------------------------------------------------------------
def run_chessboard_calibration(
    output_path: Path,
    board_size: tuple[int, int] = (9, 6),
    square_size_mm: float = 25.0,
    num_images: int = 15,
    resolution: tuple[int, int] = (1280, 720),
    show_preview: bool = True,
) -> None:
    """對著印出來的棋盤格拍 num_images 張照片，計算相機內參矩陣與畸變係數。

    board_size 是「內角點」數量（例如標準 10x7 方格棋盤格的內角點是 9x6），
    square_size_mm 是每格實際邊長，用來讓輸出的外參有正確的物理尺度
    （本專案只需要 camera_matrix / dist_coeffs 做去畸變，外參不是重點，
    但仍照標準流程給定 square_size 以求正確性）。

    show_preview=True（預設）會開一個即時預覽視窗：找到棋盤格時疊綠色角點與
    連線、標題列顯示已擷取張數；沒找到時疊紅字提示，方便你調整角度/距離直到
    抓到為止。這需要目前的終端機有畫面可以開視窗（本機接螢幕操作、或有 X11
    轉發的 SSH），純文字 SSH 連線（沒有 -X/-Y）開不了視窗，會自動退回無預覽
    模式並印出警告，不會讓整個校正流程失敗。
    """
    if Picamera2 is None or cv2 is None:
        raise RuntimeError("需要 picamera2 與 opencv-python 才能執行校正")

    objp = np.zeros((board_size[0] * board_size[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0 : board_size[0], 0 : board_size[1]].T.reshape(-1, 2)
    objp *= square_size_mm

    objpoints: list[np.ndarray] = []
    imgpoints: list[np.ndarray] = []

    picam2 = Picamera2()
    # 跟 CameraStream.start() 用同一套 preview_configuration + align() 路徑，
    # 而不是 create_video_configuration——避免兩條 stream role 的顏色/畫面行為
    # 不一致（實測發現過這兩種設定路徑即使給一樣的 format 字串，結果可能不同）。
    picam2.preview_configuration.main.size = resolution
    picam2.preview_configuration.main.format = "RGB888"
    picam2.preview_configuration.align()
    picam2.configure("preview")
    picam2.start()
    time.sleep(0.5)

    window_name = "SmartCart 相機校正預覽（q 結束 / Ctrl+C 也可）"
    preview_active = show_preview
    if preview_active:
        try:
            cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        except cv2.error as exc:
            print(f"[警告] 無法開啟預覽視窗（{exc}），改用無畫面模式繼續校正。")
            print("       如果是用 SSH 連線，試試 `ssh -X` 或 `ssh -Y` 帶著 X11 轉發重連。")
            preview_active = False

    print(f"開始拍攝校正影像，目標 {num_images} 張。對著棋盤格移動角度/距離，")
    if preview_active:
        print("預覽視窗裡看到綠色角點連線代表偵測成功，會自動擷取；按 q 可提前結束。")
    else:
        print("（無預覽模式）每偵測到一組角點就自動存一張，按 Ctrl+C 可提前結束。")

    gray_shape: Optional[tuple[int, int]] = None
    try:
        captured = 0
        while captured < num_images:
            frame = picam2.capture_array()
            # 色彩通道順序假設同 CameraStream 類別docstring——目前當作可以直接
            # 用（不轉換）。灰階偵測角點對顏色不敏感，就算這個假設是錯的，也
            # 不影響棋盤格偵測本身；但下面預覽視窗顯示的顏色會受影響，用
            # --color-test 驗證清楚比較保險。
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray_shape = gray.shape[::-1]
            found, corners = cv2.findChessboardCorners(gray, board_size, None)

            if preview_active:
                # 複製一份再畫，避免 drawChessboardCorners/putText 的原地繪製動到
                # picamera2 內部緩衝區。已經是 BGR，cv2.imshow 可以直接吃。
                display_frame = frame.copy()
                if found:
                    cv2.drawChessboardCorners(display_frame, board_size, corners, found)
                    status_text, status_color = "FOUND - capturing...", (0, 220, 0)
                else:
                    status_text, status_color = "not found - adjust angle/distance", (0, 0, 220)
                cv2.putText(
                    display_frame, status_text, (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, status_color, 2, cv2.LINE_AA,
                )
                cv2.putText(
                    display_frame, f"captured: {captured}/{num_images}", (20, 80),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA,
                )
                cv2.imshow(window_name, display_frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    print("使用者按 q，提前結束擷取。")
                    break

            if found:
                criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
                corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
                objpoints.append(objp)
                imgpoints.append(corners)
                captured += 1
                print(f"  已擷取 {captured}/{num_images}")
                time.sleep(0.5)  # 避免連續兩張幾乎同一個姿態
            else:
                time.sleep(0.05 if preview_active else 0.1)
    except KeyboardInterrupt:
        print("提前結束擷取。")
    finally:
        picam2.stop()
        picam2.close()
        if preview_active:
            cv2.destroyWindow(window_name)

    if len(objpoints) < 5:
        raise RuntimeError(f"有效角點資料太少（只有 {len(objpoints)} 張），無法可靠校正，請重新拍攝")

    assert gray_shape is not None
    ret, camera_matrix, dist_coeffs, _rvecs, _tvecs = cv2.calibrateCamera(
        objpoints, imgpoints, gray_shape, None, None
    )
    reprojection_error = ret
    print(f"校正完成，reprojection error = {reprojection_error:.4f}（越接近 0 越好，一般 < 1.0 算可接受）")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(str(output_path), camera_matrix=camera_matrix, dist_coeffs=dist_coeffs)
    print(f"已存到 {output_path}")


def run_color_test(output_path: Path, resolution: tuple[int, int] = (1280, 720)) -> None:
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

    picam2 = Picamera2()
    picam2.preview_configuration.main.size = resolution
    picam2.preview_configuration.main.format = "RGB888"
    picam2.preview_configuration.align()
    picam2.configure("preview")
    picam2.start()
    time.sleep(1.0)  # 讓 AE/AWB 收斂

    try:
        frame = picam2.capture_array()
    finally:
        picam2.stop()
        picam2.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # 刻意不做任何顏色轉換：這張圖存出來的樣子，就是目前程式碼「假設不用轉換」
    # 這個前提的直接驗證結果。
    cv2.imwrite(str(output_path), frame)
    print(f"已存到 {output_path}（沒有做任何顏色轉換，原封不動存的）")
    print("請把這張圖傳回來給我看，或自己打開來對照鏡頭前物品的實際顏色：")
    print("  - 顏色正常（例如藍色的東西看起來是藍色）→ 目前程式碼不用改")
    print("  - 顏色對調（例如藍色的東西看起來是橘紅色）→ 需要加一次 RGB<->BGR 轉換")


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="相機串流 / 校正工具")
    parser.add_argument("--calibrate", action="store_true", help="執行棋盤格校正流程")
    parser.add_argument("--preview", action="store_true", help="單純測試擷取影格並印出 shape")
    parser.add_argument(
        "--color-test",
        action="store_true",
        help="拍一張照片原封不動存成 PNG，用來明確驗證色彩通道順序（RGB vs BGR）對不對",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="校正時不開預覽視窗（沒有 X11 畫面可用時加這個，例如純文字 SSH）",
    )
    parser.add_argument(
        "--color-test-output", default="color_test.png",
        help="--color-test 拍下來的圖片存到哪（預設存在目前工作目錄的 color_test.png）。"
             "注意 --output 是校正結果 .npz 的路徑，兩個是不同的東西，不要混用",
    )
    parser.add_argument("--num-images", type=int, default=15)
    parser.add_argument("--board-cols", type=int, default=9, help="棋盤格內角點欄數")
    parser.add_argument("--board-rows", type=int, default=6, help="棋盤格內角點列數")
    parser.add_argument("--square-size-mm", type=float, default=25.0)
    parser.add_argument(
        "--output", default="vision/camera_calibration.npz", help="校正結果輸出路徑"
    )
    args = parser.parse_args()

    if args.calibrate:
        run_chessboard_calibration(
            output_path=Path(args.output),
            board_size=(args.board_cols, args.board_rows),
            square_size_mm=args.square_size_mm,
            num_images=args.num_images,
            show_preview=not args.no_window,
        )
        return

    if args.color_test:
        run_color_test(output_path=Path(args.color_test_output))
        return

    if args.preview:
        stream = CameraStream(calibration_file=args.output)
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
