"""
core/recording.py

UART 校正錄音（秤重、光流、航向共用）。格式跟門口的 core/gate/recorder.py 同一個
概念：每一筆原始封包一行、標上「目前在做哪一步」，參數與分段時間放在旁邊的
.meta.json，事後可以完整重算（各校正工具的 --from-data）。

檔案：
    <目錄>/samples.csv       每一筆 UART 封包一行
    <目錄>/samples.meta.json 種類、開始時間、各段的 [label, t0, t1] 與其他資訊

CSV 欄位：
    segment, epoch, dx, dy, squal, yaw_deg, pitch_deg, roll_deg, hx711_raw

錄音用背景執行緒 UartTap 一直把 receiver.out_queue 抽乾（佇列上限 200 筆＝10 秒，
不抽會掉資料）；label 是 None 的時候（兩步之間、等使用者按 Enter）收到的封包丟掉。
"""

from __future__ import annotations

import csv
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

COLUMNS = ["segment", "epoch", "dx", "dy", "squal", "yaw_deg", "pitch_deg", "roll_deg", "hx711_raw"]


@dataclass
class Sample:
    t: float
    dx: int
    dy: int
    squal: int
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    hx711_raw: int

    @classmethod
    def from_packet(cls, p) -> "Sample":
        return cls(p.timestamp, p.dx, p.dy, p.squal, p.yaw_deg, p.pitch_deg, p.roll_deg, p.hx711_raw)


@dataclass
class Segment:
    label: str
    t0: float
    t1: float
    info: dict = field(default_factory=dict)    # 例如這一步放的砝碼重量、量好的距離


@dataclass
class Recording:
    kind: str
    segments: List[Segment]
    samples: Dict[str, List[Sample]]           # label -> 該段的封包（依時間）
    meta: dict

    def get(self, label: str) -> List[Sample]:
        return self.samples.get(label, [])

    def segs(self, prefix: str) -> List[Segment]:
        return [s for s in self.segments if s.label.startswith(prefix)]


def new_session_dir(root: Path) -> Path:
    return root / f"{datetime.now():%Y%m%d-%H%M%S}"


class UartTap(threading.Thread):
    """背景收封包：標上目前的段落名稱，存在記憶體裡並寫進 CSV。"""

    def __init__(self, receiver, csv_path: Path, kind: str, extra_meta: Optional[dict] = None):
        super().__init__(daemon=True, name="UartTap")
        self.receiver = receiver
        self.csv_path = Path(csv_path)
        self.meta_path = self.csv_path.with_suffix(".meta.json")
        self.kind = kind
        self.extra_meta = dict(extra_meta or {})
        self.label: Optional[str] = None
        self.samples: Dict[str, List[Sample]] = {}
        self.segments: List[Segment] = []
        self.last: Optional[Sample] = None      # 最新一筆（不論有沒有在錄），給畫面即時顯示用
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.csv_path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(COLUMNS)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                p = self.receiver.out_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            s = Sample.from_packet(p)
            self.last = s
            label = self.label
            if label is None:
                continue
            with self._lock:
                self.samples.setdefault(label, []).append(s)
                self._writer.writerow([label, f"{s.t:.6f}", s.dx, s.dy, s.squal, f"{s.yaw_deg:.3f}",
                                       f"{s.pitch_deg:.3f}", f"{s.roll_deg:.3f}", s.hx711_raw])

    # ------------------------------------------------------------------
    def begin(self, label: str) -> float:
        """開始一段。開始前先把佇列裡累積的舊封包丟掉（那是等 Enter 期間收到的）。"""
        while True:
            try:
                self.receiver.out_queue.get_nowait()
            except queue.Empty:
                break
        t0 = time.time()
        self.label = label
        self._t0 = t0
        return t0

    def end(self, **info) -> Segment:
        """結束目前這一段。info 會存進 meta（例如 mass_g=500）。"""
        time.sleep(0.15)          # 讓已經在路上的最後幾筆進來
        label = self.label
        self.label = None
        seg = Segment(label, self._t0, time.time(), dict(info))
        self.segments.append(seg)
        self.write_meta()
        return seg

    def since(self, label: str, start: int) -> List[Sample]:
        """這一段第 start 筆之後的封包（即時畫面一次拿新進來的）。"""
        with self._lock:
            return list(self.samples.get(label, [])[start:])

    def count(self, label: Optional[str] = None) -> int:
        with self._lock:
            return len(self.samples.get(label or self.label or "", []))

    def write_meta(self, **more) -> None:
        meta = {
            "kind": self.kind,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "segments": [[s.label, s.t0, s.t1, s.info] for s in self.segments],
            **self.extra_meta,
            **more,
        }
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def recording(self) -> Recording:
        with self._lock:
            return Recording(self.kind, list(self.segments), {k: list(v) for k, v in self.samples.items()},
                             {"kind": self.kind, **self.extra_meta})

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=2.0)
        with self._lock:
            self._file.flush()
            self._file.close()


def wait_for_stream(receiver, timeout_sec: float = 5.0) -> bool:
    """確認 UART 真的有封包進來（接不上就直接報錯，不要讓人空等）。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not receiver.out_queue.empty():
            return True
        time.sleep(0.1)
    return False


def load(path: Path) -> Recording:
    """讀回錄音。path 可以是目錄或 samples.csv。"""
    path = Path(path)
    csv_path = path / "samples.csv" if path.is_dir() else path
    meta_path = csv_path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    samples: Dict[str, List[Sample]] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            samples.setdefault(row["segment"], []).append(Sample(
                float(row["epoch"]), int(row["dx"]), int(row["dy"]), int(row["squal"]),
                float(row["yaw_deg"]), float(row["pitch_deg"]), float(row["roll_deg"]), int(row["hx711_raw"])))
    segs = [Segment(l, float(a), float(b), dict(i or {})) for l, a, b, i in meta.get("segments", [])]
    if not segs:   # 沒有 meta：用每段頭尾
        segs = [Segment(k, v[0].t, v[-1].t, {}) for k, v in samples.items() if v]
        segs.sort(key=lambda s: s.t0)
    return Recording(meta.get("kind", ""), segs, samples, meta)


def save_result(out_dir: Path, result: dict) -> None:
    (Path(out_dir) / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=_jsonable) + "\n", encoding="utf-8")


def _jsonable(o):
    if hasattr(o, "__dict__"):
        return o.__dict__
    if isinstance(o, tuple):
        return list(o)
    return str(o)


def countdown(seconds: float, tap: UartTap, text: str) -> None:
    """靜止量測的倒數，同時顯示收到幾筆（看得到資料有沒有在進來）。"""
    t0 = time.time()
    while True:
        left = seconds - (time.time() - t0)
        if left <= 0:
            break
        print(f"\r    {text}，剩 {left:4.1f} 秒（已收 {tap.count()} 筆）   ", end="", flush=True)
        time.sleep(0.2)
    print()


def segment_stats_line(samples: List[Sample]) -> Tuple[int, float]:
    if len(samples) < 2:
        return len(samples), 0.0
    dur = samples[-1].t - samples[0].t
    return len(samples), (len(samples) - 1) / dur if dur > 0 else 0.0
