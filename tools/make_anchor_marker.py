"""
tools/make_anchor_marker.py

產生定位點的 ArUco 標記圖（DICT_4X4_50），印出來平貼在長廊末端定位點的地上（方向不拘），
印的時候要保持正方形（取消「符合框架」、縮放 100%）。推車會壓過去的話建議護貝。

印好之後量「黑色方框」的實際邊長（不含白邊），填 config.json：
    "vision": { "anchor_marker_id": 0, "anchor_marker_size_m": 0.15 }
app 用這個邊長解標記的位置（core/store/vision.py 的 solvePnP），量錯的話推車位置會整體縮放、算出來的
相機高度也會跟 vision.camera_height_m 對不上而被當成誤判。

用法：
    python3 -m tools.make_anchor_marker                    # 編號 0，存成 data/anchor_marker_0.png
    python3 -m tools.make_anchor_marker --id 3 --out marker.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from core import config  # noqa: E402
from core.store.vision import make_marker  # noqa: E402



def main() -> int:
    ap = argparse.ArgumentParser(description="產生定位點 ArUco 標記圖")
    ap.add_argument("--id", type=int, default=0, help="標記編號（0~49），要跟 config.json 的 vision.anchor_marker_id 一樣")
    ap.add_argument("--out", default=None, help="輸出檔名，預設 data/anchor_marker_<id>.png")
    ap.add_argument("--pixels", type=int, default=1200, help="黑框邊長的像素數（越大印起來越清楚）")
    args = ap.parse_args()
    out = Path(args.out) if args.out else config.DATA_DIR / f"anchor_marker_{args.id}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), make_marker(args.id, args.pixels))
    print(f"已存成 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
