"""
core/landmark_correction.py

Phase 3/7：用低成本 BLE Beacon 當「離散地標校正點」，同時解決兩件事：
    1. Phase 4 目前缺的「進入/走出管制區」偵測——core/cart_state_machine.py
       的 GateEntryDetected/GateExitDetected 事件目前完全沒有硬體來源。
    2. Phase 7「浮動座標系怎麼對齊店面地圖」的初始定位/持續漂移校正機制——
       之前完全擱置，因為沒有機制可以把 dead-reckoning 的累積座標釘回絕對
       座標（見 core/position_types.py 開頭原本的說明）。

設計依據：這是跟使用者討論另一份 Gemini 對話紀錄（賣場防盜門與標籤技術解
析）後定案的方向。那份討論最後收斂出的關鍵結論是——不要試圖用 RFID/BLE 的
訊號強度（RSSI）算連續座標：室內多路徑反射環境下 RSSI 可以抖動到
±10~15 dBm，換算距離誤差達 2~4 公尺，直接拿來當座標會比原本沒校正的 IMU
漂移還糟。正確做法是把它當成「離散的、已知絕對座標的觸發點」——車子經過
Beacon 附近時偵測到一個訊號峰值，就把 dead-reckoning 累積的座標強制校正/
融合回這個地標的已知座標。這跟 core/odometry_engine.py 用 squal 過濾掉不
可信的光流讀數、（還沒寫的）vanishing_point.py 用視覺校正 yaw，是同一種
「用不完美但夠格的訊號做離散/局部修正，不取代主要定位來源」的設計哲學。

跟現有硬體限制的對應：MCU（SAM D21）腳位已經用滿，所以這裡不經過 MCU，
直接用 Pi5 內建藍牙掃描 Beacon，Beacon 本身放在門口/走道固定點，車體不需
要加裝任何感測器或接線，成本幾乎是零。

兩種校正情境，設計成兩個獨立、但共用平滑演算法的偵測器：

1. **一般地標校正點**（走道轉角、貨架節點……Phase 7 之後要擴充用）：只需
   要知道「車子經過了這個點」，不需要方向資訊。用 `RssiPeakDetector`——車
   子靠近 Beacon 時 RSSI 上升，經過最近點後開始下降，偵測「從上升轉為下
   降」的那個瞬間（局部極大值），觸發一次校正事件，同一個峰值只觸發一次。

2. **管制區門口**（需要方向：進來還是出去，直接決定要不要觸發防損警報，
   判定信心要求比一般地標點高）：用 `GateCrossingDetector`——門內門外各放
   一顆 Beacon（間距約 1.5~2 公尺），比較兩者訊號強度的「相對大小」（不是
   絕對值），哪一顆訊號較強代表車子目前比較靠近哪一側；哪邊持續佔優勢翻
   轉到另一邊，就代表車子完成了一次穿越。用相對比較而不是絕對閾值，是因
   為車體金屬造成的固定衰減會同時影響兩顆 Beacon，差分可以抵消掉這個固定
   衰減，比單一 Beacon 的絕對 RSSI 閾值穩定（跟討論紀錄的結論一致）。

這個檔案裡的東西全部是純邏輯（純函式/簡單的串流狀態物件，沒有任何 I/O），
可以直接餵合成的 RSSI 數列做測試，不需要真的 BLE 硬體或 Beacon——跟這個
專案其他模組（odometry_engine.py 用假封包測試、cart_state_machine.py 用
假事件測試）一樣的哲學。真正掃描 BLE 廣播、把原始 RSSI 讀數餵進來，是
drivers/ble_beacon_scanner.py 的責任（背景執行緒 + bleak 套件），目前這個
環境沒有 BLE 硬體可以測，那支還沒寫、也還沒實測過，先不做（等這個檔案的
邏輯定案、也有真的 Beacon 可以測之後再說）。

用法（互動模擬，不需要真實硬體）：
    python3 -m core.landmark_correction --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

GATE_CROSSING_ENTERING = "entering"  # 由外側 Beacon 佔優勢翻轉成內側 Beacon 佔優勢
GATE_CROSSING_EXITING = "exiting"    # 由內側 Beacon 佔優勢翻轉成外側 Beacon 佔優勢


@dataclass
class LandmarkPoint:
    """一個已知絕對座標的地標點（一顆 Beacon 對應一個）。座標系跟
    core/position_types.py 的 PositionEstimate 一致（公釐）。這裡的座標是
    「這個 Beacon 實際安裝位置」的絕對座標，要實測填入，不是佔位值就能用。
    """

    beacon_id: str
    x_mm: float
    y_mm: float
    label: str = ""


# ----------------------------------------------------------------------
# 共用：RSSI 平滑
# ----------------------------------------------------------------------
def smooth_rssi(history: List[float], window: int = 5) -> float:
    """對最近 window 筆原始 RSSI 讀數做移動中位數平滑，用來消除單筆突波
    （室內多路徑反射造成的瞬間跳動）。history 是時間順序的原始讀數，取最後
    window 筆算中位數；不足 window 筆時用目前有的全部樣本（開機/剛進入偵測
    範圍時樣本還不夠多，用現有的先頂著，不要因為樣本不足就完全不給值）。
    """
    if not history:
        raise ValueError("history 不能是空的")
    recent = history[-window:]
    sorted_vals = sorted(recent)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return float(sorted_vals[mid])
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


# ----------------------------------------------------------------------
# 一般地標點：峰值偵測
# ----------------------------------------------------------------------
class RssiPeakDetector:
    """單顆 Beacon 的「峰值偵測」——持續餵平滑後的 RSSI 值，內部追蹤『目前是
    上升還是下降趨勢』，偵測到『剛從上升轉為下降』的那個瞬間，回報一次校正
    事件——同一個峰值只會回報一次，不會每筆都重複觸發。

    重要限制（後來發現原本的設計漏掉的東西，記錄下來避免以後又犯同樣的錯）：
    只看「有沒有上升又下降」是不夠的——車子在 Beacon 附近晃過去（例如逛旁邊
    的貨架、靠近一下又走開，完全沒有真的通過那個地標點）一樣會產生「訊號由
    小到大再變小」的曲線，形狀上跟真的通過沒有差別，`min_rise_dbm`（上升
    幅度的相對門檻）擋不住這種情況，因為晃近一點點也能滿足「上升超過 3dB」。

    所以這裡多加一個 `min_peak_rssi_dbm`（絕對門檻，非必填）：峰值本身的
    RSSI 數值必須夠強（代表車子當下真的夠靠近，例如 -65dBm 以上），才算數；
    只是「相對變強」但峰值本身還是很弱（例如從 -95 升到 -88 又降回去，代表
    車子從很遠的地方稍微靠近了一點點但從沒真的接近），不會觸發校正。這樣
    設計是刻意的：對「一般地標校正點」來說，只要車子當下真的夠靠近某個已知
    座標的 Beacon，那一刻把座標校正過去就是準的，不管車子後續往哪裡走、有
    沒有停留晃動——所以「晃近又晃走」如果真的靠得夠近，觸發校正並不算錯，
    只是「靠近但沒真的到位」時不該觸發，這就是絕對門檻要擋的情況。
    """

    def __init__(self, min_rise_dbm: float = 3.0, min_peak_rssi_dbm: Optional[float] = None):
        self.min_rise_dbm = min_rise_dbm
        self.min_peak_rssi_dbm = min_peak_rssi_dbm
        self._prev_value: Optional[float] = None
        self._prev_timestamp: Optional[float] = None
        self._rising_from_value: Optional[float] = None
        self._armed = False

    def add_sample(self, smoothed_rssi: float, timestamp: float) -> Optional[float]:
        """回傳這次呼叫是否確認了一個峰值：回傳峰值當下的 timestamp（也就
        是上一筆樣本的時間，因為這一筆已經是開始下降的第一筆），沒有確認
        峰值則回傳 None。
        """
        peak_timestamp = None
        if self._prev_value is not None:
            if smoothed_rssi > self._prev_value:
                if self._rising_from_value is None:
                    self._rising_from_value = self._prev_value
                if smoothed_rssi - self._rising_from_value >= self.min_rise_dbm:
                    self._armed = True
            elif smoothed_rssi < self._prev_value:
                if self._armed:
                    peak_value = self._prev_value
                    if self.min_peak_rssi_dbm is None or peak_value >= self.min_peak_rssi_dbm:
                        peak_timestamp = self._prev_timestamp
                    # 沒過絕對門檻的話這波峰值就當作沒發生（不觸發校正），
                    # 但這波「上升->下降」的循環本身已經結束，狀態一樣要重
                    # 置，才能繼續偵測下一波真正靠近的峰值。
                self._armed = False
                self._rising_from_value = None
            # 相等：持平，不改變狀態（可能是還沒開始動，也可能是雜訊）

        self._prev_value = smoothed_rssi
        self._prev_timestamp = timestamp
        return peak_timestamp

    def reset(self) -> None:
        self._prev_value = None
        self._prev_timestamp = None
        self._rising_from_value = None
        self._armed = False


# ----------------------------------------------------------------------
# 管制區門口：雙 Beacon 差分方向判定
# ----------------------------------------------------------------------
class GateCrossingDetector:
    """門口雙 Beacon 差分判定——比較『門內 Beacon A』與『門外 Beacon B』平滑
    後的 RSSI 相對大小，偵測『誰佔優勢』翻轉的瞬間，判斷車子穿越門口的方向。

    這裡的判定結果會直接決定要不要觸發防損警報/鎖輪，誤判的代價比一般地標
    點高很多，所以比 RssiPeakDetector 多兩層防護（詳細原因見下面）：

    1. `hysteresis_dbm`：要求差值先超過這個門檻的其中一邊、再翻到另一邊也
       超過門檻，才算一次有效的翻轉；差值還在模糊區間時不改變判定，避免兩
       者訊號很接近時（例如車子停在門口正中間）差值在 0 附近抖動造成反覆
       誤判方向。
    2. `min_crossing_rssi_dbm`（絕對門檻，非必填）：只看「A 比 B 強」或「B
       比 A 強」是不夠的——車子在店裡別的地方閒逛，剛好比較靠近門內 Beacon
       一點點（兩邊訊號可能都很弱，但 A 還是比 B 強），一樣會被判定成
       「靠門內」，跟真的站在門口附近沒有兩樣。這個絕對門檻要求「目前佔優
       勢的那顆 Beacon，訊號本身也要夠強」，代表車子當下真的在門口附近的
       偵測範圍內，不是在店裡隨便什麼地方剛好相對比較近。
    3. `min_confirm_samples`（連續樣本數，預設 1 代表不啟用）：要求新的優
       勢方連續出現這麼多筆平滑後的樣本才算數，用來過濾單一雜訊樣本造成的
       瞬間翻轉，設成大於 1（例如 3）可以再增加一點抗雜訊能力，代價是判定
       會晚個幾筆樣本的時間。

    誠實的限制：這三層防護能大幅降低『在門口附近晃過但沒有真的穿越』被誤判
    的機率，但沒辦法做到完全消除——BLE RSSI 本身是全向、無方向性的訊號強
    弱量測，物理上量不到『有沒有真的通過那個實體開口』，這是這個方案（相對
    於光電閘門、地埋迴路線這種要求物理上真的通過某個窄通道才會觸發的機制）
    先天就有的模糊地帶。要進一步降低誤判，實務上還需要兩件事，這支類別本
    身做不到，要靠上層（呼叫這個類別的地方）補：
        (a) 部署時把 Beacon 發射功率調低，讓兩顆 Beacon 的有效偵測範圍實際
            上就侷限在門口通道附近，店裡其他地方訊號弱到連 hysteresis 都
            過不了，從實體佈署層面縮小模糊地帶。
        (b) 用 `is_heading_consistent()`（見下面）拿 IMU 的航向角做交叉驗
            證——只有當車子當下的朝向跟『真的在穿越門口』該有的朝向大致一
            致時，才真正採信這次穿越判定。這是討論紀錄裡 Gemini 自己也強
            調的『關鍵』步驟，之前第一版實作漏掉了，這裡補上對應的工具函式，
            但實際『要不要採信』的判斷要在使用這個類別的地方（之後接
            main.py 時）把兩個獨立訊號 AND 起來，這支類別只負責 BLE 這一半。
    """

    def __init__(
        self,
        hysteresis_dbm: float = 2.0,
        min_crossing_rssi_dbm: Optional[float] = None,
        min_confirm_samples: int = 1,
    ):
        self.hysteresis_dbm = hysteresis_dbm
        self.min_crossing_rssi_dbm = min_crossing_rssi_dbm
        self.min_confirm_samples = max(1, min_confirm_samples)
        self._dominant: Optional[str] = None  # "a" 或 "b"：目前已確認佔優勢的一邊
        self._candidate: Optional[str] = None
        self._candidate_count = 0

    def add_sample(self, smoothed_rssi_a: float, smoothed_rssi_b: float, timestamp: float) -> Optional[str]:
        """回傳 GATE_CROSSING_ENTERING / GATE_CROSSING_EXITING（確認了一次
        穿越），或 None（還沒有新的穿越事件）。
        """
        diff = smoothed_rssi_a - smoothed_rssi_b  # > 0 代表比較靠近門內（A）
        if diff >= self.hysteresis_dbm:
            sample_dominant, dominant_rssi = "a", smoothed_rssi_a
        elif diff <= -self.hysteresis_dbm:
            sample_dominant, dominant_rssi = "b", smoothed_rssi_b
        else:
            sample_dominant, dominant_rssi = None, None  # 差值還在模糊區間

        if sample_dominant is None:
            self._candidate = None
            self._candidate_count = 0
            return None

        if self.min_crossing_rssi_dbm is not None and dominant_rssi < self.min_crossing_rssi_dbm:
            # 相對上是佔優勢沒錯，但訊號本身太弱，代表車子根本不在門口附近
            # 的偵測範圍內，不採信這一筆。
            self._candidate = None
            self._candidate_count = 0
            return None

        if sample_dominant == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = sample_dominant
            self._candidate_count = 1

        crossing = None
        if self._candidate_count >= self.min_confirm_samples:
            if self._dominant is not None and self._candidate != self._dominant:
                crossing = GATE_CROSSING_EXITING if self._dominant == "a" else GATE_CROSSING_ENTERING
            self._dominant = self._candidate

        return crossing

    def reset(self) -> None:
        self._dominant = None
        self._candidate = None
        self._candidate_count = 0


def is_heading_consistent(yaw_deg: float, expected_yaw_deg: float, tolerance_deg: float = 45.0) -> bool:
    """Gate 穿越判定的第二道防線（見 GateCrossingDetector 文件說明）：BLE
    訊號本身沒有方向性，量不到『有沒有真的通過那個實體開口』，所以
    GateCrossingDetector 判定出方向之後，還要拿 IMU 的航向角（core.
    odometry_engine 累積出來的 yaw_deg）做交叉驗證——車子當下的朝向要跟
    『真的在往這個方向穿越門口』理論上該有的朝向大致一致，才真正採信。

    expected_yaw_deg：往這個方向穿越門口，車頭理論上該朝的角度（度，跟
    core/position_types.py 的座標系一致），由門的實際安裝方向決定，要在
    config.json 設定（進門跟出門通常差 180 度）。tolerance_deg 是容許的
    角度誤差，車子不會完全垂直對準門口走，預設給 45 度的寬容範圍。
    """
    diff = abs((yaw_deg - expected_yaw_deg + 180.0) % 360.0 - 180.0)
    return diff <= tolerance_deg


# ----------------------------------------------------------------------
def load_landmark_config() -> dict:
    """讀 config.json 的 "landmarks" 區塊，回傳地標點列表、門口 Beacon 配對、
    平滑/偵測參數。座標目前是 CALIBRATE_ME 佔位值，要等 Beacon 實際安裝位置
    定案、實測填入才是準確值——這點跟 weight/odometry 的校正值是一樣的性質。
    """
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    cfg = raw.get("landmarks", {})
    points = [
        LandmarkPoint(beacon_id=p["beacon_id"], x_mm=p["x_mm"], y_mm=p["y_mm"], label=p.get("label", ""))
        for p in cfg.get("points", [])
    ]
    return {
        "points": points,
        "gate_beacon_pair": cfg.get("gate_beacon_pair", {}),
        "rssi_smoothing_window": cfg.get("rssi_smoothing_window", 5),
        "peak_min_rise_dbm": cfg.get("peak_min_rise_dbm", 3.0),
        "peak_min_rssi_dbm": cfg.get("peak_min_rssi_dbm"),
        "gate_hysteresis_dbm": cfg.get("gate_hysteresis_dbm", 2.0),
        "gate_min_crossing_rssi_dbm": cfg.get("gate_min_crossing_rssi_dbm"),
        "gate_min_confirm_samples": cfg.get("gate_min_confirm_samples", 1),
        "gate_exit_yaw_deg": cfg.get("gate_exit_yaw_deg"),
        "gate_heading_tolerance_deg": cfg.get("gate_heading_tolerance_deg", 45.0),
    }


# ----------------------------------------------------------------------
# 互動模擬 CLI——沒有真的 BLE 硬體/Beacon 可以測，這裡讓使用者手動輸入一串
# RSSI 數值（模擬推車經過 Beacon 時訊號先升後降的過程），驗證峰值偵測跟門口
# 雙 Beacon 方向判定的邏輯是否合理。
# ----------------------------------------------------------------------
def _run_peak_simulation(min_rise_dbm: float, min_peak_rssi_dbm: Optional[float], window: int) -> None:
    detector = RssiPeakDetector(min_rise_dbm=min_rise_dbm, min_peak_rssi_dbm=min_peak_rssi_dbm)
    history: List[float] = []
    t = 0.0
    print("=== 一般地標點峰值偵測模擬 ===")
    print(f"（絕對門檻 min_peak_rssi_dbm={min_peak_rssi_dbm}：峰值不夠強會被忽略，試試看只晃到 -85 又降回去 vs. 真的靠近到 -60）")
    print("依序輸入 RSSI 數值（dBm，例如 -80），模擬推車靠近再遠離 Beacon 的過程。輸入 q 結束。")
    while True:
        raw = input("RSSI: ").strip()
        if raw.lower() == "q":
            break
        try:
            rssi = float(raw)
        except ValueError:
            print("請輸入數字或 q")
            continue
        history.append(rssi)
        smoothed = smooth_rssi(history, window=window)
        t += 1.0
        peak_ts = detector.add_sample(smoothed, t)
        print(f"  平滑後={smoothed:.1f}dBm", end="")
        if peak_ts is not None:
            print(f"  >>> 偵測到峰值！觸發地標校正（t={peak_ts:.0f}）")
        else:
            print()


def _run_gate_simulation(
    hysteresis_dbm: float,
    min_crossing_rssi_dbm: Optional[float],
    min_confirm_samples: int,
    window: int,
) -> None:
    detector = GateCrossingDetector(
        hysteresis_dbm=hysteresis_dbm,
        min_crossing_rssi_dbm=min_crossing_rssi_dbm,
        min_confirm_samples=min_confirm_samples,
    )
    history_a: List[float] = []
    history_b: List[float] = []
    t = 0.0
    print("=== 管制區門口雙 Beacon 方向判定模擬 ===")
    print(f"（絕對門檻 min_crossing_rssi_dbm={min_crossing_rssi_dbm}，連續樣本數 min_confirm_samples={min_confirm_samples}）")
    print("依序輸入 'A的RSSI B的RSSI'（例如 -70 -85），模擬推車通過門口。輸入 q 結束。")
    while True:
        raw = input("RSSI_A RSSI_B: ").strip()
        if raw.lower() == "q":
            break
        parts = raw.split()
        if len(parts) != 2:
            print("請輸入兩個數字，用空格分開")
            continue
        try:
            rssi_a, rssi_b = float(parts[0]), float(parts[1])
        except ValueError:
            print("請輸入數字或 q")
            continue
        history_a.append(rssi_a)
        history_b.append(rssi_b)
        smoothed_a = smooth_rssi(history_a, window=window)
        smoothed_b = smooth_rssi(history_b, window=window)
        t += 1.0
        crossing = detector.add_sample(smoothed_a, smoothed_b, t)
        print(f"  平滑後 A={smoothed_a:.1f} B={smoothed_b:.1f}", end="")
        if crossing is not None:
            print(f"  >>> 偵測到穿越！方向={crossing}（t={t:.0f}）")
        else:
            print()


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Phase 3/7 BLE 地標校正邏輯")
    parser.add_argument("--simulate", choices=["peak", "gate"], help="互動模擬：peak=一般地標峰值偵測，gate=門口雙 Beacon 方向判定")
    args = parser.parse_args()

    try:
        cfg = load_landmark_config()
    except (OSError, json.JSONDecodeError):
        cfg = {
            "rssi_smoothing_window": 5,
            "peak_min_rise_dbm": 3.0,
            "peak_min_rssi_dbm": None,
            "gate_hysteresis_dbm": 2.0,
            "gate_min_crossing_rssi_dbm": None,
            "gate_min_confirm_samples": 1,
        }

    if args.simulate == "peak":
        _run_peak_simulation(cfg["peak_min_rise_dbm"], cfg["peak_min_rssi_dbm"], cfg["rssi_smoothing_window"])
    elif args.simulate == "gate":
        _run_gate_simulation(
            cfg["gate_hysteresis_dbm"],
            cfg["gate_min_crossing_rssi_dbm"],
            cfg["gate_min_confirm_samples"],
            cfg["rssi_smoothing_window"],
        )
    else:
        print("請指定 --simulate peak 或 --simulate gate")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
