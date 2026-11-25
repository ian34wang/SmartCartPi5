"""
drivers/front_camera.py

找貨頁推車位置用的前方相機：影格 → 灰階 → 縮小 → 去畸變。給 ui/workers.py 的
VisionConsumer（定位點標記、貨架消失點，演算法在 core/store/vision.py）。

擷取共用 drivers/camera_stream.CameraStream（2026-11-23 改）：
    1. 同一條 picamera2 設定路徑（preview_configuration + align()）。棋盤格校正
       （python3 -m drivers.camera_stream --calibrate）就是走這條路徑量的；原本這裡自己用
       create_video_configuration，跟校正時的設定路徑不同。
    2. 一顆 CSI 相機同時只能被一個 Picamera2 打開，全專案只留一個開相機的地方。
    3. CameraStream 以 calibration_file=None 開（輸出原始影格），去畸變只在這裡做一次。
       CameraStream 自己的去畸變是全解析度 cv2.undistort()，每張都重算對照表；這裡先縮小、
       再用預先算好的對照表 remap，比較省。兩邊都做的話畸變會被校正兩次。

內參換算（core/store/vision.scaled_intrinsics）：
    校正解析度 (W_c, H_c) = config.json 的 camera.resolution，處理解析度 (W_p, H_p)，
    s_x = W_p / W_c、s_y = H_p / H_c：
        f_x' = s_x f_x,  c_x' = s_x c_x,  f_y' = s_y f_y,  c_y' = s_y c_y
    f_x, f_y 焦距（像素）、(c_x, c_y) 主點（像素）；畸變係數 (k_1, k_2, p_1, p_2, k_3) 是對正規化
    座標定義的，不隨解析度變。
    校正檔（vision/camera_calibration.npz）只存 camera_matrix 與 dist_coeffs，沒存影像尺寸；
    --calibrate 用的是預設 1280x720，跟 camera.resolution 相同，c_x = 637.8 ≈ W_c / 2 = 640 也對得上。
    改 camera.resolution 就要重新校正。實際影格尺寸跟 camera.resolution 不同時直接丟例外。

相機選哪一台、要不要翻轉：config.json 的 camera.index、camera.hflip、camera.vflip（見 drivers/camera_stream.py）。
去畸變時換成 cv2.getOptimalNewCameraMatrix(alpha = vision.undistort_alpha) 的新內參；self.K 是去畸變後
影像的內參，core/store/vision 的 solvePnP 與消失點都用它。

翻轉改了內參要跟著換（--flip-calibration 或 --calibrate），校正檔記著量的時候的翻轉，對不上就報錯。

硬體接不上一律丟例外，由 app 啟動流程報錯結束（不提供沒有相機的替代路徑）。
"""

from __future__ import annotations

from typing import Tuple

import numpy as np

from core import config


# 影格超過這麼久沒更新就當相機斷了（CameraStream 背景執行緒 30 fps 在跑）
_STALE_SEC = 1.0


class FrontCamera:
    def __init__(self, camera_cfg: dict, proc_width: int = 640, undistort_alpha: float = 0.0):
        import cv2

        from core.store.vision import scaled_intrinsics
        from drivers.camera_stream import CameraStream

        self._cv2 = cv2
        res = tuple(camera_cfg.get("resolution", [1280, 720]))
        self.calib_size: Tuple[int, int] = (int(res[0]), int(res[1]))
        self.size: Tuple[int, int] = (int(proc_width), int(round(proc_width * res[1] / res[0])))

        calib = config.ROOT / camera_cfg.get("calibration_file", "vision/camera_calibration.npz")
        if not calib.exists():
            raise FileNotFoundError(f"找不到相機校正檔 {calib}（python3 -m drivers.camera_stream --calibrate 產生）")
        data = np.load(calib)
        from drivers.camera_stream import calibration_flip
        want = (bool(camera_cfg.get("hflip", False)), bool(camera_cfg.get("vflip", False)))
        have = calibration_flip(data)
        if have != want:
            raise RuntimeError(
                f"校正檔 {calib} 是在 hflip={have[0]}、vflip={have[1]} 下量的，config.json 的 camera 是 "
                f"hflip={want[0]}、vflip={want[1]}，內參對不上（同一台相機：python3 -m drivers.camera_stream "
                "--flip-calibration 換算；換了相機：--calibrate 重拍）")
        K_raw, self.dist = scaled_intrinsics(data["camera_matrix"], data["dist_coeffs"], self.calib_size, self.size)
        # 去畸變後的影像用新的內參 K（cv2.getOptimalNewCameraMatrix）：alpha = 0 只留每個像素都有來源的範圍
        # （沒有黑邊，四個角落會被裁掉一些），alpha = 1 原始影像每一個像素都留著（有黑邊，但中央的焦距
        # 變小、標記變小）。之後的 solvePnP、消失點都用這個 K。
        self.K, _roi = cv2.getOptimalNewCameraMatrix(K_raw, self.dist, self.size, float(undistort_alpha), self.size)
        self._map1, self._map2 = cv2.initUndistortRectifyMap(K_raw, self.dist, None, self.K, self.size, cv2.CV_16SC2)

        self._stream = CameraStream(
            resolution=self.calib_size,
            framerate=int(camera_cfg.get("framerate", 30)),
            calibration_file=None,
            pixel_format=camera_cfg.get("format", "RGB888"),
            camera_num=int(camera_cfg.get("index", 0)),
            hflip=bool(camera_cfg.get("hflip", False)),
            vflip=bool(camera_cfg.get("vflip", False)),
        )
        self._stream.start()
        try:
            want_sensor = tuple(int(x) for x in data["sensor_size"]) if "sensor_size" in data else None
            got_sensor = self._stream.sensor_size_used
            if got_sensor is not None and want_sensor != tuple(got_sensor):
                raise RuntimeError(
                    f"校正檔 {calib} 是在感測器模式 {want_sensor or '（舊檔沒記，2026-11-26 前的裁切模式）'} 量的，"
                    f"現在相機用的是 {tuple(got_sensor)}（完整視野），視野不同內參就不同：重新校正 "
                    "python3 -m drivers.camera_stream --calibrate --square-size-mm <格子邊長>")
            frame = self._stream.get_latest_frame(timeout=3.0)
            if frame is None or frame.size == 0:
                raise RuntimeError("相機開起來了，但 3 秒內拿不到影格")
            h, w = frame.shape[:2]
            if (w, h) != self.calib_size:
                raise RuntimeError(
                    f"相機實際影格 {w}x{h}，跟 camera.resolution {self.calib_size[0]}x{self.calib_size[1]}"
                    "（校正檔量的解析度）不同，內參對不上")
        except Exception:
            self._stream.stop()
            raise

    def read_gray(self) -> np.ndarray:
        """一張去畸變後的灰階影像（self.size）。picamera2 的 RGB888 記憶體順序是 B, G, R。"""
        cv2 = self._cv2
        age = self._stream.frame_age()
        if age is None or age > _STALE_SEC:
            raise RuntimeError(f"前方相機影格停在 {age if age is None else round(age, 1)} 秒前（相機斷線？）")
        frame = self._stream.get_latest_frame(timeout=_STALE_SEC)
        if frame is None:
            raise RuntimeError("前方相機拿不到影格")
        if frame.ndim == 2:
            gray = frame
        else:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY if frame.shape[2] == 4 else cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, self.size, interpolation=cv2.INTER_AREA)
        return cv2.remap(gray, self._map1, self._map2, cv2.INTER_LINEAR)

    def stop(self) -> None:
        self._stream.stop()
