"""
tools/_common.py

各工具共用的小函式（終端機互動與輸出）。只給 tools/ 底下的腳本用。
"""

from __future__ import annotations

import queue
import unicodedata
from typing import Optional, Tuple


def drain(q: "queue.Queue") -> None:
    """丟掉佇列裡累積的舊資料（等使用者按 Enter 的期間收到的）。"""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def text_width(s: str) -> int:
    """終端機顯示寬度：中日韓全形字算 2 格。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, w: int) -> str:
    """補空白到顯示寬度 w（表格對齊用）。"""
    return s + " " * max(w - text_width(s), 0)


def parse_float(raw: str) -> Optional[float]:
    try:
        return float(raw.strip())
    except ValueError:
        return None


def parse_pair(raw: str) -> Optional[Tuple[float, float]]:
    """"3,0" / "3 0" / "1.2, -0.5" / "1，2" → 兩個數字。空字串或看不懂回 None。"""
    parts = raw.replace("，", ",").replace(",", " ").split()
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def ask_float(prompt: str, skip_note: str = "跳過") -> Optional[float]:
    """問一個數字；直接 Enter 或看不懂回 None。"""
    raw = input(prompt).strip()
    if not raw:
        return None
    v = parse_float(raw)
    if v is None:
        print(f"  不是數字，{skip_note}。")
    return v
