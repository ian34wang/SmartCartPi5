"""
core/gate_monitor.py

把 `drivers/ble_beacon_scanner.py` 送來的一筆一筆 BLE RSSI 觀測，變成
「進場／出場」判定，給 `core/cart_state_machine.py` 的 `GateEntryDetected`／
`GateExitDetected` 用。

為什麼要獨立成一支：`core/landmark_correction.py` 的 `GateCrossingDetector`
只負責「兩顆 Beacon 平滑後的 RSSI 誰佔優勢、什麼時候翻轉」這一段純判定，
它不知道 RSSI 從哪來、也不做平滑、更不碰 IMU 航向。中間這些事
（每顆 Beacon 各自維護一段歷史、各自平滑、湊成一對餵給偵測器、再用航向角
做交叉驗證）原本散在「之後接 main.py 時再做」的口頭約定裡，沒有實作；
`ui/app_gui.py` 跟 `tools/run_real_hardware_flow.py` 兩邊都需要，所以收斂成
這一支共用，不要各寫一份。

判定的濾波與規則本身寫在 `core/gate_model.py`（單一標準）：這支只是把那套
規則包成「一筆一筆即時處理」的介面，順便把每一筆讀到的值與判定結果記錄下來
（`core/gate_recorder.py`），之後可以用 `tools/analyze_gate_log.py` 重播分析。
校正工具挑參數時重播的也是同一套規則，所以校正時驗證過的行為就是實際的行為。

航向交叉驗證（這是 BLE 判定之外的第二道獨立訊號）：
    BLE RSSI 是全向性的，物理上量不到「有沒有真的通過那個實體開口」，所以
    門口判定除了 BLE 差分之外，還要拿 IMU 的航向角交叉驗證——車子當下的朝向
    要跟「真的在穿越門口」該有的朝向大致一致，才採信這次判定。
    只有在 config.json 的 `landmarks.gate_exit_yaw_deg` **有填實測值**時才會
    啟用；還沒量的話那個欄位是 null，這一層就自動不啟用（拿一個沒校正過的
    角度去擋，只會把真的穿越也一起擋掉）。量到之後填進去就會自動生效。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from core.gate_model import GateDecision, GateParams, SmoothingWindow
from core.landmark_correction import GATE_CROSSING_ENTERING, GATE_CROSSING_EXITING

logger = logging.getLogger(__name__)


class GateMonitor:
    """門口雙 Beacon 監看器（core/gate_model.py 那套規則的即時版本）。

    用法：
        monitor = GateMonitor.from_config(landmark_cfg, recorder=GateRecorder.for_runtime("app"))
        # 每收到一筆 BLE 觀測就餵進來
        crossing = monitor.process_observation(obs.beacon_id, obs.rssi, obs.timestamp)
        if crossing == "entering": ... # 送 GateEntryDetected
        # UART 每收到一包就更新航向（給交叉驗證用）
        monitor.update_heading(packet.yaw_deg)
        # 結束時
        monitor.close()
    """

    def __init__(
        self,
        inside_beacon_id: str,
        outside_beacon_id: str,
        smoothing_sec: float = 1.0,
        smoothing_method: str = "p75",
        min_samples: int = 3,
        hysteresis_dbm: float = 4.0,
        min_crossing_rssi_dbm: Optional[float] = None,
        min_confirm_samples: int = 1,
        expected_exit_yaw_deg: Optional[float] = None,
        heading_tolerance_deg: float = 45.0,
        rssi_offsets: Optional[Dict[str, float]] = None,
        recorder=None,
    ):
        if not inside_beacon_id or not outside_beacon_id:
            raise ValueError(
                "門口內外側的 beacon_id 都必須指定"
                "（config.json 的 landmarks.gate_beacon_pair.inside_id / outside_id）"
            )
        self.params = GateParams(
            inside_id=inside_beacon_id,
            outside_id=outside_beacon_id,
            smoothing_sec=smoothing_sec,
            estimator=smoothing_method,
            min_samples=max(1, min_samples),
            hysteresis_dbm=hysteresis_dbm,
            min_crossing_rssi_dbm=min_crossing_rssi_dbm,
            min_confirm_samples=max(1, min_confirm_samples),
            offsets=dict(rssi_offsets or {}),
            exit_yaw_deg=expected_exit_yaw_deg,
            heading_tolerance_deg=heading_tolerance_deg,
        )
        self._window = SmoothingWindow(inside_beacon_id, outside_beacon_id, smoothing_sec,
                                       smoothing_method, min_samples)
        self._decision = GateDecision(self.params)
        self._current_yaw_deg: Optional[float] = None
        self.skipped_too_few_count = 0
        self.recorder = recorder
        if recorder is not None:
            recorder.start(self.params)

    @classmethod
    def from_config(cls, cfg: dict, recorder=None) -> "GateMonitor":
        """用 `core.landmark_correction.load_landmark_config()` 的結果建構。"""
        return cls.from_params(GateParams.from_landmark_config(cfg), recorder=recorder)

    @classmethod
    def from_params(cls, p: GateParams, recorder=None) -> "GateMonitor":
        return cls(
            inside_beacon_id=p.inside_id,
            outside_beacon_id=p.outside_id,
            smoothing_sec=p.smoothing_sec,
            smoothing_method=p.estimator,
            min_samples=p.min_samples,
            hysteresis_dbm=p.hysteresis_dbm,
            min_crossing_rssi_dbm=p.min_crossing_rssi_dbm,
            min_confirm_samples=p.min_confirm_samples,
            expected_exit_yaw_deg=p.exit_yaw_deg,
            heading_tolerance_deg=p.heading_tolerance_deg,
            rssi_offsets=p.offsets,
            recorder=recorder,
        )

    # ------------------------------------------------------------------
    # 舊介面保留（其他模組與測試在用）
    @property
    def inside_beacon_id(self) -> str:
        return self.params.inside_id

    @property
    def outside_beacon_id(self) -> str:
        return self.params.outside_id

    @property
    def smoothing_sec(self) -> float:
        return self.params.smoothing_sec

    @property
    def smoothing_method(self) -> str:
        return self.params.estimator

    @property
    def min_samples(self) -> int:
        return self.params.min_samples

    @property
    def rssi_offsets(self) -> Dict[str, float]:
        return self.params.offsets

    @property
    def expected_exit_yaw_deg(self) -> Optional[float]:
        return self.params.exit_yaw_deg

    @property
    def heading_tolerance_deg(self) -> float:
        return self.params.heading_tolerance_deg

    @property
    def rejected_by_heading_count(self) -> int:
        return self._decision.rejected_by_heading

    @property
    def _history(self):
        return self._window.history

    @property
    def _detector(self):
        return self._decision.detector

    @property
    def heading_check_enabled(self) -> bool:
        """航向交叉驗證有沒有啟用（`gate_exit_yaw_deg` 有填實測值才會啟用）。"""
        return self.params.exit_yaw_deg is not None

    # ------------------------------------------------------------------
    def update_heading(self, yaw_deg: float) -> None:
        """餵進最新的 IMU 航向角（UART 封包的 yaw_deg）。"""
        self._current_yaw_deg = yaw_deg

    def process_observation(self, beacon_id: str, rssi: float, timestamp: float) -> Optional[str]:
        """處理一筆 BLE 觀測，回傳 "entering"／"exiting"／None。

        規則見 core/gate_model.py 開頭的判定流程。兩顆 Beacon 窗內都至少要有
        rssi_min_samples 筆才判定——只有一邊有訊號的時候，「誰比較強」沒有意義。
        """
        if beacon_id not in self._window.history:
            return None  # 不是門口那兩顆（是走道地標點），這裡不管

        pair = self._window.add(beacon_id, rssi, timestamp)
        if pair is None:
            # 樣本太少的平滑值不可信：剛進入偵測範圍、或某一顆快要收不到時都會發生。
            self.skipped_too_few_count += 1
            self._record(timestamp, beacon_id, rssi, None)
            return None

        step = self._decision.step(timestamp, pair[0], pair[1], self._current_yaw_deg)
        if step.rejected:
            logger.warning(
                "BLE 判定為 %s，但 IMU 航向角 %.1f 度跟預期的穿越方向不符，不採信這次判定"
                "（累計擋下 %d 次）",
                "entering" if step.dominant == "a" else "exiting",
                self._current_yaw_deg, self.rejected_by_heading_count,
            )
        self._record(timestamp, beacon_id, rssi, step)
        return step.event

    def _record(self, t, beacon_id, rssi, step) -> None:
        if self.recorder is not None:
            self.recorder.record_observation(t, beacon_id, rssi, self._current_yaw_deg, step)

    def close(self) -> None:
        """結束時呼叫，把記錄檔寫完。"""
        if self.recorder is not None:
            self.recorder.close()

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """目前這一刻的判定依據，給診斷用（不改變任何狀態）。

        回傳濾波後（已加偏移）的兩個值、差分值、目前判定在哪一側，以及絕對門檻過不過。
        `drivers/ble_beacon_scanner.py` 的 CLI 印的是**原始**讀數，肉眼看起來抖動很大；
        真正拿去判定的是這裡濾波後的值。要看濾波後的樣子用
        `tools/calibrate_gate_beacons.py --monitor`。
        """
        n_in, n_out = self._window.counts()
        out = {
            "inside": None, "outside": None, "diff": None,
            "dominant": self._decision.dominant,
            "n_inside": n_in, "n_outside": n_out,
            "above_abs_threshold": None,
        }
        pair = self._window.current()
        if pair is not None:
            si = pair[0] + self.params.offset_inside
            so = pair[1] + self.params.offset_outside
            out["inside"], out["outside"], out["diff"] = si, so, si - so
            thr = self.params.min_crossing_rssi_dbm
            if thr is not None:
                out["above_abs_threshold"] = max(si, so) >= thr
        return out

    def describe(self) -> str:
        """一行文字說明目前的設定，開機時印出來用。"""
        heading = (
            f"航向交叉驗證啟用（預期出場朝向 {self.expected_exit_yaw_deg:.0f}±"
            f"{self.heading_tolerance_deg:.0f} 度）"
            if self.heading_check_enabled
            else "航向交叉驗證未啟用（config.json 的 landmarks.gate_exit_yaw_deg 還沒填實測值）"
        )
        offs = ", ".join(
            f"{bid}{self.rssi_offsets.get(bid, 0.0):+.1f}dB"
            for bid in (self.inside_beacon_id, self.outside_beacon_id)
        )
        uncalibrated = all(
            abs(self.rssi_offsets.get(bid, 0.0)) < 1e-9
            for bid in (self.inside_beacon_id, self.outside_beacon_id)
        )
        warn = (
            "　[警告] 兩顆的 RSSI 偏移量都是 0，代表還沒做過門口校正——"
            "不同廠牌/設定的 Beacon 發射功率可能差 10 dB 以上，"
            "未校正時判定結果可能跟實際位置相反。請跑 tools/calibrate_gate_beacons.py"
            if uncalibrated else ""
        )
        rec = f"　記錄檔：{self.recorder.path}" if self.recorder is not None else ""
        return (
            f"門口 Beacon：內側={self.inside_beacon_id} 外側={self.outside_beacon_id}　"
            f"平滑={self.smoothing_sec:.1f}s/{self.smoothing_method}　"
            f"遲滯={self.params.hysteresis_dbm:.1f}dB　"
            f"校正偏移={offs}　{heading}{warn}{rec}"
        )


__all__ = ["GateMonitor", "GATE_CROSSING_ENTERING", "GATE_CROSSING_EXITING"]
