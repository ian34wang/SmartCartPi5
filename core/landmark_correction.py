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
沒有 CLI、也沒有互動模擬入口。

實際的資料流是：
    drivers/ble_beacon_scanner.py   真的掃 BLE 廣播，產生一筆筆 RSSI 觀測
        -> core/gate_monitor.py     每顆 Beacon 各自平滑、湊成一對餵進下面的
                                    GateCrossingDetector，並用 IMU 航向交叉驗證
        -> core/cart_state_machine  GateEntryDetected / GateExitDetected
"""

from __future__ import annotations

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
    rssi_offset_db: float = 0.0
    """這顆 Beacon 的 RSSI 校正偏移量，比較訊號強弱之前會先加上去。

    為什麼需要：門口的差分判定假設「訊號比較強的那顆＝比較近的那顆」，但這
    個假設只有在兩顆 Beacon 的實際發射功率/天線增益一樣時才成立。實測（2026-09-15，
    兩顆不同廠牌的 Beacon）發現：車子明明離 OUTSIDE 只有 1.7m、離 INSIDE 有
    3.6m，INSIDE 卻還比 OUTSIDE 強 6 dB——單看訊號強弱會得到完全相反的結論，
    系統性偏差高達 12.5 dB。不校正的話出場判定永遠不會觸發。

    用 `tools/calibrate_gate_beacons.py` 量出來後寫回 config.json。
    """


# ----------------------------------------------------------------------
# 共用：RSSI 平滑
# ----------------------------------------------------------------------
RSSI_ESTIMATORS = ("p75", "p90", "median", "max", "mean")


def smooth_rssi(history: List[float], window: int = 5, method: str = "p75") -> float:
    """把最近 window 筆原始 RSSI 讀數壓成一個代表值，消除多路徑造成的抖動。

    ## 為什麼預設不是中位數（2026-09-15 用實測資料改的）

    一般在講「RSSI 要平滑」時直覺都是中位數或平均，但實測資料顯示這裡的雜訊
    **是單邊的**，不是對稱的：

        GATE-INSIDE   中位數 -64　往上最多 +1 dB（2 筆）　往下最多 -6 dB（5 筆）
        GATE-OUTSIDE  中位數 -70　往上最多 +1 dB（11 筆）　往下最多 -6 dB（15 筆）

    訊號很少突然「變強」，但常常突然「變弱」——這是多路徑破壞性干涉（deep fade）
    的典型特徵：反射波跟直達波相位相反時會互相抵消。換句話說，**上包絡線（最強
    的那幾筆）才接近真正的直達路徑，下面那些是被抵消掉的假訊號**。

    中位數對「對稱雜訊」是好選擇，但對單邊雜訊反而會被下半部的 fade 拉著跑；
    更糟的是當樣本呈現雙峰分布（一半正常、一半在 fade 中）時，中位數會在兩個
    群之間跳來跳去。實測 window=5 時 GATE-INSIDE 的中位數殘餘標準差高達 2.29 dB，
    是所有估計量裡最差的。

    各估計量在「車子完全靜止」時的殘餘抖動（越小越好，window=5）：

        估計量      INSIDE    OUTSIDE   較差的那個
        mean         0.89      0.80       0.89
        median       2.29      0.60       2.29   <- 最差
        p75          0.00      0.53       0.53
        p90          0.29      0.46       0.46   <- 最好
        max          0.49      0.49       0.49

    換算成「需要多大的 ΔRSSI 變化才可靠（3σ）」：median 要 7.1 dB、mean 要 3.6 dB、
    **p75 只要 1.6 dB**。差了 4 倍以上——等於門口 Beacon 的間距可以放寬一倍多。

    預設選 p75 而不是 p90/max 的理由：max 只看單一最強樣本，一筆假的高讀數就會
    主導結果；p75 丟掉下面 75% 的 fade，但仍然用到 1/4 的樣本，單筆異常不會決定
    結果，穩健性比較好。

    method 可選 "p75"（預設）/"p90"/"median"/"max"/"mean"。
    不足 window 筆時用目前有的全部樣本（剛進入偵測範圍時樣本還不夠多，
    用現有的先頂著，不要因為樣本不足就完全不給值）。
    """
    if not history:
        raise ValueError("history 不能是空的")
    recent = sorted(history[-window:])
    n = len(recent)

    if method == "mean":
        return sum(recent) / n
    if method == "max":
        return float(recent[-1])
    if method == "median":
        mid = n // 2
        return float(recent[mid]) if n % 2 == 1 else (recent[mid - 1] + recent[mid]) / 2.0
    if method in ("p75", "p90"):
        q = 0.75 if method == "p75" else 0.90
        k = (n - 1) * q
        lo = int(k)
        hi = min(lo + 1, n - 1)
        return recent[lo] + (recent[hi] - recent[lo]) * (k - lo)
    raise ValueError(f"不認得的 RSSI 估計量：{method!r}（可用：{RSSI_ESTIMATORS}）")


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
        LandmarkPoint(
            beacon_id=p["beacon_id"], x_mm=p["x_mm"], y_mm=p["y_mm"],
            label=p.get("label", ""), rssi_offset_db=p.get("rssi_offset_db", 0.0),
        )
        for p in cfg.get("points", [])
    ]
    return {
        "points": points,
        "rssi_offsets": {p.beacon_id: p.rssi_offset_db for p in points},
        "gate_beacon_pair": cfg.get("gate_beacon_pair", {}),
        "rssi_smoothing_sec": cfg.get("rssi_smoothing_sec", 0.8),
        "rssi_estimator": cfg.get("rssi_estimator", "p75"),
        "rssi_min_samples": cfg.get("rssi_min_samples", 2),
        "peak_min_rise_dbm": cfg.get("peak_min_rise_dbm", 3.0),
        "peak_min_rssi_dbm": cfg.get("peak_min_rssi_dbm"),
        "gate_hysteresis_dbm": cfg.get("gate_hysteresis_dbm", 2.0),
        "gate_min_crossing_rssi_dbm": cfg.get("gate_min_crossing_rssi_dbm"),
        "gate_min_confirm_samples": cfg.get("gate_min_confirm_samples", 1),
        "gate_exit_yaw_deg": cfg.get("gate_exit_yaw_deg"),
        "gate_heading_tolerance_deg": cfg.get("gate_heading_tolerance_deg", 45.0),
        "gate_max_sample_age_sec": cfg.get("gate_max_sample_age_sec", 5.0),
    }
