"""
core/config.py

專案路徑與 config.json 的唯一讀寫點。其他模組不自己拼路徑、不自己開 config.json。

    ROOT          專案根目錄
    CONFIG_PATH   config.json
    DATA_DIR      data/（校正錄音、執行期記錄、軌跡圖）

config.json 裡底線開頭的鍵（"_comment"、"_xxx_comment"）是給人看的註解，section() 會拿掉。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"

# 前方相機視覺（config.json 的 vision 區塊沒寫的欄位用這些；意義見 core/store/vision.py）
VISION_DEFAULTS: Dict[str, float] = {
    "anchor_marker_id": 0,          # 定位點 ArUco 標記編號（DICT_4X4_50）
    "anchor_marker_size_m": 0.15,   # 標記黑框實際邊長（m）
    "anchor_trigger_m": 1.5,        # 標記離推車水平距離在這以內才採用
    "camera_height_m": 0.5,         # 相機離地高度
    "height_tol_m": 0.15,           # 由標記算出的相機高度跟上面差超過這個就當誤判
    "vp_yaw_weight": 0.2,           # 消失點修航向的互補濾波權重 w
    "vp_min_lines": 4,              # 消失點至少要幾條線支持
    "vp_max_angle_deg": 28.0,       # 消失點在光軸左右幾度內才採用
    "fps": 5.0,                     # 每秒處理幾張
    "proc_width": 640,              # 縮到這個寬度再處理
    "undistort_alpha": 0.0,         # 去畸變保留多少視野：0 = 不留黑邊（角落裁掉一些），1 = 全部留（中央變小）
}


def load(path: Path = CONFIG_PATH) -> dict:
    """整份 config.json（含註解鍵）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(cfg: dict, path: Path = CONFIG_PATH) -> None:
    Path(path).write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def strip_comments(d: Optional[dict]) -> dict:
    """拿掉底線開頭的註解鍵（只拿最外層）。"""
    return {k: v for k, v in (d or {}).items() if not k.startswith("_")}


def section(name: str, cfg: Optional[dict] = None) -> dict:
    """config.json 某個區塊，拿掉註解鍵。cfg 不給就現讀。"""
    return strip_comments((cfg if cfg is not None else load()).get(name))


def vision(cfg: Optional[dict] = None) -> dict:
    """vision 區塊套在 VISION_DEFAULTS 上。"""
    return {**VISION_DEFAULTS, **section("vision", cfg)}


def update_section(name: str, values: Dict[str, object], comments: Optional[Dict[str, str]] = None,
                   path: Path = CONFIG_PATH) -> None:
    """只改 config.json 某個區塊裡的這幾個值（校正工具寫回用），其他欄位與註解都留著。"""
    cfg = load(path)
    sec = cfg.setdefault(name, {})
    sec.update(values)
    sec.update(comments or {})
    save(cfg, path)


def rel(p) -> str:
    """印給人看的路徑：在專案裡就印相對路徑。"""
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)
