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
from typing import Dict, List, Optional, Tuple

from core.landmark_correction import (
    GATE_CROSSING_ENTERING,
    GATE_CROSSING_EXITING,
    GateCrossingDetector,
    is_heading_consistent,
    smooth_rssi,
)

logger = logging.getLogger(__name__)


class GateMonitor:
    """門口雙 Beacon 監看器。

    用法：
        monitor = GateMonitor.from_config(landmark_cfg)
        # 每收到一筆 BLE 觀測就餵進來
        crossing = monitor.process_observation(obs.beacon_id, obs.rssi, obs.timestamp)
        if crossing == "entering": ... # 送 GateEntryDetected
        # IMU 航向角（UART 封包裡的 yaw_deg）持續餵進來做交叉驗證
        monitor.update_heading(packet.yaw_deg)
    """

    def __init__(
        self,
        inside_beacon_id: str,
        outside_beacon_id: str,
        smoothing_sec: float = 0.4,
        smoothing_method: str = "p75",
        min_samples: int = 3,
        hysteresis_dbm: float = 2.0,
        min_crossing_rssi_dbm: Optional[float] = None,
        min_confirm_samples: int = 1,
        expected_exit_yaw_deg: Optional[float] = None,
        heading_tolerance_deg: float = 45.0,
        rssi_offsets: Optional[Dict[str, float]] = None,
    ):
        if not inside_beacon_id or not outside_beacon_id:
            raise ValueError(
                "門口內外側的 beacon_id 都必須指定"
                "（config.json 的 landmarks.gate_beacon_pair.inside_id / outside_id）"
            )
        self.inside_beacon_id = inside_beacon_id
        self.outside_beacon_id = outside_beacon_id
        # 平滑窗口用「時間」算，不是「筆數」；長度直接對應判定延遲。
        #
        # 窗口長度怎麼定：p75 這類取上包絡線的估計量，在訊號**下降**時會黏在
        # 窗口內最強（最舊）的那一筆，所以延遲大約就是一整個窗口長度。換算成
        # 距離就是「推車速度 × 窗口秒數」——1 m/s 配 0.4 秒 = 判定會晚 40 cm。
        # 所以：
        #     smoothing_sec ≈ 可接受的判定延遲距離 ÷ 推車速度
        # 這個延遲是兩邊對稱的（都用同一個估計量），所以只會讓判定變晚，
        # 不會判錯方向——是安全的失效方向。
        #
        # 窗口縮短的代價是樣本數變少、雜訊壓不住，所以**廣播間隔一定要先調快**
        # （20~30 ms），才有辦法在 0.4 秒內收到足夠的筆數。這兩件事是綁在一起的。
        #
        #
        # 至於為什麼是「時間」不是「筆數」：實測兩顆 Beacon 的廣播間隔差了 3 倍
        # （OUTSIDE 43 筆的時間內 INSIDE 只有 14 筆）——同樣是 5 筆，INSIDE 涵蓋
        # 的實際時間是 OUTSIDE 的 3 倍。也就是兩邊的平滑值代表的是不同時刻的
        # 訊號強度，慢的那邊系統性地落後。推車靜止時看不出來，但正好在穿越門口、
        # 訊號快速變化的那幾秒，這個落後會直接扭曲差分值——而那正是唯一需要它
        # 準確的時候。改成時間窗之後兩邊永遠代表同一段時間。
        self.smoothing_sec = smoothing_sec
        self.smoothing_method = smoothing_method
        self.min_samples = max(1, min_samples)
        self.expected_exit_yaw_deg = expected_exit_yaw_deg
        self.heading_tolerance_deg = heading_tolerance_deg

        # 每顆 Beacon 的 RSSI 校正偏移量。差分判定假設「訊號比較強＝比較近」，
        # 這只有在兩顆的實際發射功率/天線增益一樣時才成立——實測兩顆不同廠牌的
        # Beacon 差了 12.5 dB，不校正的話判定結果會跟實際位置相反。
        # 詳見 LandmarkPoint.rssi_offset_db 與 tools/calibrate_gate_beacons.py。
        self.rssi_offsets: Dict[str, float] = dict(rssi_offsets or {})

        self._detector = GateCrossingDetector(
            hysteresis_dbm=hysteresis_dbm,
            min_crossing_rssi_dbm=min_crossing_rssi_dbm,
            min_confirm_samples=min_confirm_samples,
        )
        # {beacon_id: [(timestamp, 校正後的 rssi), ...]}
        self._history: Dict[str, List[Tuple[float, float]]] = {
            inside_beacon_id: [], outside_beacon_id: [],
        }
        self._last_seen: Dict[str, float] = {}
        self._current_yaw_deg: Optional[float] = None
        self.rejected_by_heading_count = 0
        self.skipped_too_few_count = 0

    @classmethod
    def from_config(cls, cfg: dict) -> "GateMonitor":
        """用 `core.landmark_correction.load_landmark_config()` 的結果建構。"""
        pair = cfg.get("gate_beacon_pair", {})
        return cls(
            inside_beacon_id=pair.get("inside_id", ""),
            outside_beacon_id=pair.get("outside_id", ""),
            smoothing_sec=cfg.get("rssi_smoothing_sec", 0.4),
            smoothing_method=cfg.get("rssi_estimator", "p75"),
            min_samples=cfg.get("rssi_min_samples", 3),
            hysteresis_dbm=cfg.get("gate_hysteresis_dbm", 2.0),
            min_crossing_rssi_dbm=cfg.get("gate_min_crossing_rssi_dbm"),
            min_confirm_samples=cfg.get("gate_min_confirm_samples", 1),
            expected_exit_yaw_deg=cfg.get("gate_exit_yaw_deg"),
            heading_tolerance_deg=cfg.get("gate_heading_tolerance_deg", 45.0),
            rssi_offsets=cfg.get("rssi_offsets"),
        )

    # ------------------------------------------------------------------
    @property
    def heading_check_enabled(self) -> bool:
        """航向交叉驗證有沒有啟用（`gate_exit_yaw_deg` 有填實測值才會啟用）。"""
        return self.expected_exit_yaw_deg is not None

    def update_heading(self, yaw_deg: float) -> None:
        """餵進最新的 IMU 航向角（UART 封包的 yaw_deg）。"""
        self._current_yaw_deg = yaw_deg

    def process_observation(self, beacon_id: str, rssi: float, timestamp: float) -> Optional[str]:
        """處理一筆 BLE 觀測，回傳 "entering"／"exiting"／None。

        兩顆 Beacon 都至少要有一筆讀數才開始判定——只有一邊有訊號的時候，
        「誰比較強」這個問題根本沒有意義。
        """
        if beacon_id not in self._history:
            return None  # 不是門口那兩顆（是走道地標點），這裡不管

        # 先套上這顆 Beacon 的校正偏移量，後面所有比較都用校正後的值
        adjusted = float(rssi) + self.rssi_offsets.get(beacon_id, 0.0)
        self._history[beacon_id].append((timestamp, adjusted))
        self._last_seen[beacon_id] = timestamp

        # 兩邊都只保留時間窗內的樣本（順便限制長度上限，避免時間戳異常時暴增）
        cutoff = timestamp - self.smoothing_sec
        for bid, hist in self._history.items():
            keep_from = 0
            for idx, (ts, _) in enumerate(hist):
                if ts >= cutoff:
                    keep_from = idx
                    break
            else:
                keep_from = len(hist)
            if keep_from:
                del hist[:keep_from]
            if len(hist) > 200:
                del hist[:-200]

        inside_hist = [v for _, v in self._history[self.inside_beacon_id]]
        outside_hist = [v for _, v in self._history[self.outside_beacon_id]]
        if len(inside_hist) < self.min_samples or len(outside_hist) < self.min_samples:
            # 樣本太少的平滑值不可信。這在「剛進入偵測範圍」跟「某一顆快要收不到」
            # 時都會發生，兩種情況下都不該做方向判定。
            self.skipped_too_few_count += 1
            return None

        crossing = self._detector.add_sample(
            smooth_rssi(inside_hist, len(inside_hist), self.smoothing_method),
            smooth_rssi(outside_hist, len(outside_hist), self.smoothing_method),
            timestamp,
        )
        if crossing is None:
            return None

        if not self._heading_agrees(crossing):
            self.rejected_by_heading_count += 1
            logger.warning(
                "BLE 判定為 %s，但 IMU 航向角 %.1f 度跟預期的穿越方向不符，不採信這次判定"
                "（累計擋下 %d 次）",
                crossing, self._current_yaw_deg, self.rejected_by_heading_count,
            )
            return None

        return crossing

    def _heading_agrees(self, crossing: str) -> bool:
        """航向交叉驗證。沒啟用、或目前還沒有航向讀數時一律放行。"""
        if not self.heading_check_enabled:
            return True
        if self._current_yaw_deg is None:
            # UART 還沒送來任何姿態資料。這時候擋下來只會讓門口完全失效，
            # 而且「沒有航向資料」本身是 UART 那一側的問題，不是門口的問題。
            return True
        expected = self.expected_exit_yaw_deg
        if crossing == GATE_CROSSING_ENTERING:
            expected = expected + 180.0  # 進場跟出場是相反方向
        return is_heading_consistent(self._current_yaw_deg, expected, self.heading_tolerance_deg)

    def snapshot(self) -> dict:
        """目前這一刻的判定依據，給診斷用（不改變任何狀態）。

        回傳濾波後的兩個值、差分值、目前判定在哪一側，以及絕對門檻過不過。
        `drivers/ble_beacon_scanner.py` 的 CLI 印的是**原始**讀數（那支是驅動層，
        故意不做處理），肉眼看起來抖動很大；真正拿去判定的是這裡濾波後的值，
        兩者差很多。要看濾波後的樣子用 `tools/calibrate_gate_beacons.py --monitor`。
        """
        out = {
            "inside": None, "outside": None, "diff": None,
            "dominant": self._detector._dominant,
            "n_inside": len(self._history[self.inside_beacon_id]),
            "n_outside": len(self._history[self.outside_beacon_id]),
            "above_abs_threshold": None,
        }
        ih = [v for _, v in self._history[self.inside_beacon_id]]
        oh = [v for _, v in self._history[self.outside_beacon_id]]
        if len(ih) >= self.min_samples and len(oh) >= self.min_samples:
            si = smooth_rssi(ih, len(ih), self.smoothing_method)
            so = smooth_rssi(oh, len(oh), self.smoothing_method)
            out["inside"], out["outside"], out["diff"] = si, so, si - so
            thr = self._detector.min_crossing_rssi_dbm
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
        return (
            f"門口 Beacon：內側={self.inside_beacon_id} 外側={self.outside_beacon_id}　"
            f"平滑={self.smoothing_sec:.1f}s/{self.smoothing_method}　"
            f"校正偏移={offs}　{heading}{warn}"
        )


__all__ = ["GateMonitor", "GATE_CROSSING_ENTERING", "GATE_CROSSING_EXITING"]
