"""
drivers/ble_beacon_scanner.py

用 Pi 5 內建藍牙掃描 BLE Beacon 廣播，把每一筆 RSSI 讀數丟進 Queue／callback，
給 `core/gate_monitor.py` 判定管制區門口的穿越方向（進場/出場）用。

這是 `core/landmark_correction.py` 那一整套演算法唯一缺的那塊——在這支寫出來
之前，那 421 行邏輯完全接不到真實資料，`GateEntryDetected`/`GateExitDetected`
也沒有任何硬體來源。

設計上刻意「接不到就報錯」，不提供任何模擬/退回路徑：
    - 沒有裝 bleak → RuntimeError，附上安裝指令。
    - 藍牙介面卡打不開（藍牙關著、沒有權限、adapter 名稱錯） → RuntimeError。
    - 啟動後在指定時間內一顆設定檔裡的 Beacon 都沒掃到 → TimeoutError。
  這樣「明明沒在運作卻安靜地什麼都不做」這種狀況不會發生。

Beacon 怎麼認：
    每個 Beacon 在 config.json 的 `landmarks.points[]` 裡有一個 `beacon_id`
    （例如 "GATE-INSIDE"）。實際比對方式有兩種，優先用前者：
      1. `address`（該點的選填欄位）——BLE MAC 位址，最可靠，換韌體/改名都不受
         影響。用 `--list` 掃出來抄進 config.json 即可。
      2. 廣播名稱（local name）等於 `beacon_id`——如果你是拿 ESP32 自己刷
         Beacon 韌體，直接把廣播名稱設成 "GATE-INSIDE"/"GATE-OUTSIDE" 就不用
         填 address，換一顆板子也不用改設定。

用法（獨立測試）：
    python3 -m drivers.ble_beacon_scanner --list          # 掃 10 秒，列出附近所有 BLE 裝置
    python3 -m drivers.ble_beacon_scanner                 # 持續印出那幾顆的**原始** RSSI

**這支印的是未經任何處理的原始讀數**，用途是「確認 Beacon 認得到、位址對不對」。
原始 RSSI 抖動很大是正常的——實測靜止不動時峰對峰就有 7 dB，那是多路徑干涉，
不是壞掉。但真正拿去做門口判定的不是這些數字，是 `core/gate_monitor.py` 濾波後
的值（實測同一組資料，差分值的抖動從 3.2 dB 降到 0.17 dB，差 19 倍）。

要看「系統真正在用的數字」請用：
    python3 -m tools.calibrate_gate_beacons --monitor     # 原始 vs 濾波後並排顯示

Beacon 硬體選型：**發射功率越小越好**。BLE RSSI 是全向性訊號，物理上量不到
「有沒有真的通過那個實體開口」，所以要靠「有效偵測範圍本來就侷限在門口通道
附近」來幫軟體判斷分擔工作——這跟 config.json 的 `gate_min_crossing_rssi_dbm`
絕對門檻是互相配合、不是取代關係。一顆 ESP32 刷 iBeacon/Eddystone 廣播就夠用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

try:
    from bleak import BleakScanner
except ImportError:  # pragma: no cover - 讓沒有 bleak 的環境還能 import 這個模組去讀常數
    BleakScanner = None

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

_BLEAK_MISSING_MSG = (
    "沒有安裝 bleak，無法掃描 BLE Beacon。在 Pi 上執行：pip install bleak\n"
    "  （Pi OS 上還需要 BlueZ，通常系統已內建；確認藍牙有開：sudo rfkill unblock bluetooth）"
)


@dataclass
class BeaconObservation:
    """一次 BLE 廣播觀測。"""

    beacon_id: str
    rssi: int
    timestamp: float
    address: str = ""


def load_beacon_identity_map() -> Dict[str, str]:
    """從 config.json 讀出「BLE MAC 位址 -> beacon_id」的對照表。

    只收有填 `address` 的點；沒填 address 的點會改用「廣播名稱等於 beacon_id」
    的方式比對（見 `BleBeaconScanner._resolve_beacon_id`）。
    """
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f).get("landmarks", {})
    mapping: Dict[str, str] = {}
    for point in cfg.get("points", []):
        address = (point.get("address") or "").strip()
        if address:
            mapping[address.upper()] = point["beacon_id"]
    return mapping


def load_beacon_ids() -> List[str]:
    """config.json 裡所有地標點的 beacon_id。"""
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f).get("landmarks", {})
    return [p["beacon_id"] for p in cfg.get("points", [])]


class BleBeaconScanner:
    """背景執行緒持續掃描 BLE 廣播，把感興趣的 Beacon 的 RSSI 送出去。

    bleak 是 asyncio 介面，但這個專案其他的 driver（UART、條碼）都是
    「背景執行緒 + Queue」的同步介面，狀態機那邊也是同步的。為了不讓 asyncio
    這個實作細節外流到呼叫端，這裡自己在背景執行緒裡開一個 event loop 跑
    bleak，對外只暴露跟其他 driver 一樣的 `start()`/`stop()`/`out_queue`。
    """

    def __init__(
        self,
        beacon_ids: Iterable[str],
        address_map: Optional[Dict[str, str]] = None,
        out_queue: Optional["queue.Queue[BeaconObservation]"] = None,
        on_observation: Optional[Callable[[BeaconObservation], None]] = None,
        adapter: Optional[str] = None,
        start_timeout_sec: float = 10.0,
    ):
        if BleakScanner is None:
            raise RuntimeError(_BLEAK_MISSING_MSG)

        self.beacon_ids = {b.upper() for b in beacon_ids}
        self.address_map = {k.upper(): v for k, v in (address_map or {}).items()}
        self.out_queue = out_queue
        self.on_observation = on_observation
        self.adapter = adapter
        self.start_timeout_sec = start_timeout_sec

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._seen: Set[str] = set()
        self._seen_lock = threading.Lock()

    # ------------------------------------------------------------------
    def _resolve_beacon_id(self, address: str, local_name: Optional[str]) -> Optional[str]:
        """判斷這個廣播是不是我們在意的 Beacon；不是就回 None（忽略）。"""
        mapped = self.address_map.get((address or "").upper())
        if mapped is not None:
            return mapped
        if local_name and local_name.upper() in self.beacon_ids:
            return local_name
        return None

    def _on_detection(self, device, advertisement_data) -> None:
        # bleak 5.x 起 RSSI 從 advertisement_data 取（device.rssi 已標記淘汰），
        # 但舊版只有 device.rssi，兩種都接一下，免得換版本就壞掉。
        rssi = getattr(advertisement_data, "rssi", None)
        if rssi is None:
            rssi = getattr(device, "rssi", None)
        if rssi is None:
            return

        local_name = getattr(advertisement_data, "local_name", None) or getattr(device, "name", None)
        beacon_id = self._resolve_beacon_id(getattr(device, "address", ""), local_name)
        if beacon_id is None:
            return

        with self._seen_lock:
            self._seen.add(beacon_id)

        obs = BeaconObservation(
            beacon_id=beacon_id,
            rssi=int(rssi),
            timestamp=time.time(),
            address=getattr(device, "address", ""),
        )
        if self.on_observation is not None:
            self.on_observation(obs)
        if self.out_queue is not None:
            self.out_queue.put(obs)

    # ------------------------------------------------------------------
    async def _scan(self) -> None:
        try:
            kwargs = {"detection_callback": self._on_detection}
            if self.adapter:
                kwargs["adapter"] = self.adapter
            scanner = BleakScanner(**kwargs)
            await scanner.start()
        except Exception as exc:  # noqa: BLE001 — 要把原始錯誤帶回主執行緒
            self._start_error = exc
            self._ready.set()
            return

        self._ready.set()
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.2)
        finally:
            try:
                await scanner.stop()
            except Exception:  # noqa: BLE001 — 關閉失敗不該蓋掉原本的結束流程
                logger.exception("關閉 BLE 掃描時發生例外")

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._scan())
        except Exception as exc:  # noqa: BLE001
            self._start_error = exc
            self._ready.set()
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    def start(self) -> None:
        """開始掃描。藍牙打不開就直接拋 RuntimeError，不會安靜地什麼都不做。"""
        if self._thread is not None:
            raise RuntimeError("BleBeaconScanner 已經在執行中")
        self._stop_event.clear()
        self._ready.clear()
        self._start_error = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="BleBeaconScanner", daemon=True)
        self._thread.start()

        if not self._ready.wait(timeout=self.start_timeout_sec):
            self.stop()
            raise RuntimeError(
                f"BLE 掃描器啟動超過 {self.start_timeout_sec:.0f} 秒還沒就緒。"
                "確認藍牙有開（sudo rfkill unblock bluetooth）、bluetoothd 有在跑"
                "（systemctl status bluetooth）。"
            )
        if self._start_error is not None:
            err = self._start_error
            self.stop()
            raise RuntimeError(
                f"BLE 掃描器啟動失敗：{err}。"
                "確認藍牙有開（sudo rfkill unblock bluetooth）、"
                "目前使用者有權限操作藍牙（通常要在 bluetooth 群組裡，"
                "或用 sudo 跑），以及 --adapter 名稱正確（預設 hci0）。"
            ) from err
        logger.info("BLE 掃描器已啟動，正在找：%s", sorted(self.beacon_ids))

    def wait_for_beacons(self, required: Iterable[str], timeout_sec: float = 15.0) -> Set[str]:
        """等到指定的那幾顆 Beacon 都被掃到為止，逾時直接拋 TimeoutError。

        這是「開機自我檢查」用的：Beacon 沒電、放太遠、ID 打錯的話，要在啟動
        當下就講清楚，而不是等使用者推著車走過門口才發現什麼都沒發生。
        """
        required_set = set(required)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with self._seen_lock:
                missing = required_set - self._seen
            if not missing:
                return set(required_set)
            time.sleep(0.2)
        with self._seen_lock:
            missing = required_set - self._seen
            seen = set(self._seen)
        raise TimeoutError(
            f"{timeout_sec:.0f} 秒內沒有掃到這幾顆 Beacon：{sorted(missing)}"
            f"（這段期間有掃到的：{sorted(seen) or '（一顆都沒有）'}）。"
            "確認 Beacon 有電、在偵測範圍內，且 config.json 的 landmarks.points "
            "裡的 beacon_id／address 跟實際廣播出來的一致"
            "（用 `python3 -m drivers.ble_beacon_scanner --list` 查實際位址與名稱）。"
        )

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._loop = None
        logger.info("BLE 掃描器已停止")


# ----------------------------------------------------------------------
async def _list_devices(duration_sec: float, adapter: Optional[str]) -> List[tuple]:
    if BleakScanner is None:
        raise RuntimeError(_BLEAK_MISSING_MSG)
    found: Dict[str, tuple] = {}

    def _cb(device, advertisement_data):
        rssi = getattr(advertisement_data, "rssi", None)
        if rssi is None:
            rssi = getattr(device, "rssi", None)
        name = getattr(advertisement_data, "local_name", None) or getattr(device, "name", None) or "(無名稱)"
        found[device.address] = (device.address, name, rssi)

    kwargs = {"detection_callback": _cb}
    if adapter:
        kwargs["adapter"] = adapter
    scanner = BleakScanner(**kwargs)
    await scanner.start()
    await asyncio.sleep(duration_sec)
    await scanner.stop()
    return sorted(found.values(), key=lambda r: (r[2] is None, -(r[2] or 0)))


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="BLE Beacon 掃描器（獨立測試用）")
    parser.add_argument("--list", action="store_true", help="掃描一段時間，列出附近所有 BLE 裝置後結束")
    parser.add_argument("--duration", type=float, default=10.0, help="--list 掃描秒數，預設 10")
    parser.add_argument("--adapter", default=None, help="藍牙介面名稱，預設用系統預設（通常是 hci0）")
    args = parser.parse_args()

    if args.list:
        print(f"掃描 {args.duration:.0f} 秒...")
        rows = asyncio.run(_list_devices(args.duration, args.adapter))
        if not rows:
            print("一個 BLE 裝置都沒掃到。確認藍牙有開：sudo rfkill unblock bluetooth")
            return 1
        print(f"\n{'位址':<20} {'RSSI':>6}  名稱")
        print("-" * 60)
        for address, name, rssi in rows:
            print(f"{address:<20} {rssi if rssi is not None else '?':>6}  {name}")
        print(
            "\n把門口那兩顆的位址填進 config.json 的 landmarks.points[].address，"
            "或把它們的廣播名稱設成跟 beacon_id 一樣（GATE-INSIDE / GATE-OUTSIDE）。"
        )
        return 0

    beacon_ids = load_beacon_ids()
    if not beacon_ids:
        print("[錯誤] config.json 的 landmarks.points 是空的，沒有任何 beacon_id 可以找。")
        return 1

    q: "queue.Queue[BeaconObservation]" = queue.Queue()
    scanner = BleBeaconScanner(
        beacon_ids=beacon_ids,
        address_map=load_beacon_identity_map(),
        out_queue=q,
        adapter=args.adapter,
    )
    scanner.start()
    print(f"監聽中，目標 Beacon：{beacon_ids}（Ctrl+C 結束）...")
    try:
        while True:
            try:
                obs = q.get(timeout=0.5)
            except queue.Empty:
                continue
            print(f"[{obs.beacon_id:<14}] RSSI = {obs.rssi:>4} dBm   ({obs.address})")
    except KeyboardInterrupt:
        pass
    finally:
        scanner.stop()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
