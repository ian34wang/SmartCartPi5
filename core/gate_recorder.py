"""
core/gate_recorder.py

把門口判定「讀到的每一筆值」記錄下來，給事後分析與重播用。執行期（ui/app_gui.py、
tools/run_real_hardware_flow.py、校正工具的 --monitor）跟校正量測用的是同一種格式，
所以 tools/analyze_gate_log.py 兩種都能讀。

檔案：
    <名稱>.csv        每一筆 BLE 觀測一行
    <名稱>.meta.json  當時用的判定參數（core.gate_model.GateParams）與其他資訊

CSV 欄位：
    segment    段落名稱。執行期是 "run"；校正量測是位置代號（out100、thr…）
    epoch      時間戳（秒）
    beacon_id  哪一顆
    rssi       原始 RSSI（dBm，未加偏移）
    yaw_deg    當下 IMU 航向角（沒有就空白）
    s_in       門內平滑值（已加偏移；這一刻樣本不夠就空白）
    s_out      門外平滑值（已加偏移）
    diff       s_in − s_out
    dominant   這一筆之後已確認的一側：in / out / 空白（還沒確認）
    event      entering / exiting；被航向擋下記成 rejected_entering / rejected_exiting

只有前 5 欄是重播需要的；後 5 欄是當時即時算出來的結果，重播時可以拿來核對
「現在的程式算出來的，跟當時算出來的是不是一樣」。

記錄失敗（例如磁碟滿了）不會讓程式停下來——印一次警告後停止記錄，門口判定照常。
"""

from __future__ import annotations

import csv
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.gate_model import GateParams, Observation

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_LOG_ROOT = _REPO_ROOT / "data" / "gate_logs"

COLUMNS = ["segment", "epoch", "beacon_id", "rssi", "yaw_deg", "s_in", "s_out", "diff", "dominant", "event"]
_SIDE = {"a": "in", "b": "out", None: ""}
_FLUSH_SEC = 1.0


def _fmt(v: Optional[float], nd: int = 2) -> str:
    return "" if v is None else f"{v:.{nd}f}"


class GateRecorder:
    def __init__(self, csv_path: Path, kind: str, segment: str = "run", extra_meta: Optional[dict] = None):
        self.path = Path(csv_path)
        self.meta_path = self.path.with_suffix(".meta.json")
        self.kind = kind
        self.segment = segment
        self.extra_meta = dict(extra_meta or {})
        self._file = None
        self._writer = None
        self._last_flush = 0.0
        self._failed = False
        self._params: Optional[GateParams] = None
        self.rows = 0

    @classmethod
    def for_runtime(cls, kind: str) -> "GateRecorder":
        """執行期記錄檔：data/gate_logs/<日期>/<時間>_<kind>.csv，每次啟動一個新檔。"""
        now = datetime.now()
        return cls(RUNTIME_LOG_ROOT / f"{now:%Y%m%d}" / f"{now:%H%M%S}_{kind}.csv", kind=kind)

    # ------------------------------------------------------------------
    def start(self, params: GateParams) -> None:
        """記下參數；檔案等第一筆資料進來才建立（硬體沒接上就結束的話不會留下空檔案）。"""
        self._params = params

    def _open(self) -> bool:
        if self._writer is not None:
            return True
        if self._failed:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, "w", newline="", encoding="utf-8")
            self._writer = csv.writer(self._file)
            self._writer.writerow(COLUMNS)
            if self._params is not None:
                self.write_meta(self._params)
            return True
        except OSError as exc:
            self._fail(exc)
            return False

    def write_meta(self, params: GateParams, **more) -> None:
        meta = {
            "kind": self.kind,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "params": params.to_dict(),
            **self.extra_meta,
            **more,
        }
        try:
            self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            self._fail(exc)

    def record_observation(self, t: float, beacon_id: str, rssi: float, yaw: Optional[float], step,
                           segment: Optional[str] = None) -> None:
        if not self._open():
            return
        if step is None:
            tail = ["", "", "", "", ""]
        else:
            event = step.event or ""
            if step.rejected:
                event = "rejected_" + ("entering" if step.dominant == "a" else "exiting")
            tail = [_fmt(step.s_in), _fmt(step.s_out), _fmt(step.diff), _SIDE[step.dominant], event]
        try:
            self._writer.writerow([segment or self.segment, f"{t:.6f}", beacon_id, f"{float(rssi):g}",
                                   _fmt(yaw, 3)] + tail)
            self.rows += 1
            now = time.time()
            if now - self._last_flush >= _FLUSH_SEC:
                self._file.flush()
                self._last_flush = now
        except OSError as exc:
            self._fail(exc)

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
            self._writer = None

    def _fail(self, exc: Exception) -> None:
        if not self._failed:
            logger.warning("門口記錄檔寫入失敗，停止記錄（門口判定照常運作）：%s", exc)
            print(f"[警告] 門口記錄檔 {self.path} 寫入失敗，停止記錄（判定照常）：{exc}")
        self._failed = True
        self.close()


# ----------------------------------------------------------------------
def load(csv_path: Path) -> Tuple[Dict[str, List[Observation]], Dict[str, List[dict]], dict]:
    """讀記錄檔。回傳 (各段落的觀測, 各段落當時即時算出的結果列, meta)。"""
    csv_path = Path(csv_path)
    meta_path = csv_path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    obs: Dict[str, List[Observation]] = {}
    live: Dict[str, List[dict]] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            seg = row["segment"]
            yaw = float(row["yaw_deg"]) if row.get("yaw_deg") else None
            obs.setdefault(seg, []).append((float(row["epoch"]), row["beacon_id"], float(row["rssi"]), yaw))
            live.setdefault(seg, []).append(row)
    return obs, live, meta
