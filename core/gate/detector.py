"""
core/gate/detector.py

管制區門口判定的基本元件（純邏輯）：RSSI 平滑、雙 Beacon 差分的穿越偵測、航向交叉驗證。
怎麼把它們組成一套判定規則在 core/gate/model.py。

做法：門內、門外各一顆 BLE Beacon（Pi 5 內建藍牙掃描，車上不用多裝東西）。比較兩顆平滑後
RSSI 的相對大小——哪顆強代表推車靠哪一側，佔優勢的一側翻轉就是一次穿越。用差分不用絕對
門檻：車體金屬、人的遮擋造成的衰減會同時影響兩顆，相減可以抵掉。RSSI 不拿來算連續座標：
室內多路徑讓 RSSI 抖 ±10~15 dB，換成距離誤差好幾公尺。

資料流：
    drivers/ble_beacon_scanner.py  一筆筆 RSSI 觀測
      → core/gate/monitor.py       每顆各自平滑、湊成一對、判定、記錄
      → core/shopping/state_machine.py 的 GateEntryDetected / GateExitDetected
"""

from __future__ import annotations

import logging
from typing import List, Optional

from core import config

logger = logging.getLogger(__name__)

GATE_CROSSING_ENTERING = "entering"  # 外側佔優勢 → 內側佔優勢
GATE_CROSSING_EXITING = "exiting"    # 內側佔優勢 → 外側佔優勢


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
# 管制區門口：雙 Beacon 差分方向判定
# ----------------------------------------------------------------------
class GateCrossingDetector:
    """門口雙 Beacon 差分判定——比較『門內 Beacon A』與『門外 Beacon B』平滑
    後的 RSSI 相對大小，偵測『誰佔優勢』翻轉的瞬間，判斷車子穿越門口的方向。

    判定結果直接決定要不要發防損警告，所以有四層防護：

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
    4. `min_confirm_sec`（持續時間，預設 0 代表不啟用，2026-11-23 加）：新的
       優勢方除了要連續 min_confirm_samples 筆，還要從第一筆算起持續至少這麼多
       秒才確認。為什麼光數筆數不夠：判定是每收到一筆觀測就做一次，兩顆加起來
       每秒 13 筆左右，3 筆只有 0.2 秒；而平滑值（尤其 max 估計量）會被單一一筆
       特別強的封包撐住整整一個窗口，所以這 3 筆用的其實是同一筆封包——等於
       一筆封包就能確認。2026-11-16 實機記錄裡「進場 0.5 秒後又出場」就是這樣
       來的。改成看時間之後，判定需要的是「持續一段時間」，跟封包率無關。

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
            證——車子當下的朝向跟穿越門口該有的朝向大致一致時才採信
            （core/gate/model.py 的 GateDecision 把兩個訊號 AND 起來）。
    """

    def __init__(
        self,
        hysteresis_dbm: float = 2.0,
        min_crossing_rssi_dbm: Optional[float] = None,
        min_confirm_samples: int = 1,
        min_confirm_sec: float = 0.0,
    ):
        self.hysteresis_dbm = hysteresis_dbm
        self.min_crossing_rssi_dbm = min_crossing_rssi_dbm
        self.min_confirm_samples = max(1, min_confirm_samples)
        self.min_confirm_sec = max(0.0, float(min_confirm_sec or 0.0))
        self._dominant: Optional[str] = None  # "a" 或 "b"：目前已確認佔優勢的一邊
        self._candidate: Optional[str] = None
        self._candidate_count = 0
        self._candidate_since: Optional[float] = None  # 這一輪候選的第一筆時間

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
            self._candidate_since = None
            return None

        if self.min_crossing_rssi_dbm is not None and dominant_rssi < self.min_crossing_rssi_dbm:
            # 相對上是佔優勢沒錯，但訊號本身太弱，代表車子根本不在門口附近
            # 的偵測範圍內，不採信這一筆。
            self._candidate = None
            self._candidate_count = 0
            self._candidate_since = None
            return None

        if sample_dominant == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = sample_dominant
            self._candidate_count = 1
            self._candidate_since = timestamp

        crossing = None
        lasted = timestamp - self._candidate_since if self._candidate_since is not None else 0.0
        if self._candidate_count >= self.min_confirm_samples and lasted >= self.min_confirm_sec - 1e-9:
            if self._dominant is not None and self._candidate != self._dominant:
                crossing = GATE_CROSSING_EXITING if self._dominant == "a" else GATE_CROSSING_ENTERING
            self._dominant = self._candidate

        return crossing

    def reset(self) -> None:
        self._dominant = None
        self._candidate = None
        self._candidate_count = 0
        self._candidate_since = None


def is_heading_consistent(yaw_deg: float, expected_yaw_deg: float, tolerance_deg: float = 45.0) -> bool:
    """Gate 穿越判定的第二道防線（見 GateCrossingDetector 文件說明）：BLE
    訊號本身沒有方向性，量不到『有沒有真的通過那個實體開口』，所以
    GateCrossingDetector 判定出方向之後，再看 IMU 航向角跟往這個方向穿越門口該有的朝向
    是否大致一致。

    expected_yaw_deg：往這個方向穿越門口時車頭該朝的角度（度，逆時針為正），由門的安裝
    方向決定（進門與出門差 180°）。tolerance_deg：容許誤差，車子不會正對門口走。
    """
    diff = abs((yaw_deg - expected_yaw_deg + 180.0) % 360.0 - 180.0)
    return diff <= tolerance_deg


# ----------------------------------------------------------------------
def load_landmark_config(raw: Optional[dict] = None) -> dict:
    """config.json 的 landmarks 區塊（拿掉註解鍵），另外把各 Beacon 的 rssi_offset_db 整理成
    rssi_offsets = {beacon_id: 偏移}。GateParams.from_landmark_config() 吃這個結果。"""
    cfg = config.section("landmarks", raw)
    cfg["rssi_offsets"] = {p["beacon_id"]: float(p.get("rssi_offset_db", 0.0)) for p in cfg.get("points", [])}
    return cfg
