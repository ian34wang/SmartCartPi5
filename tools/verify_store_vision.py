"""
tools/verify_store_vision.py

前方相機的現場驗證：地上定位點標記算出來的距離、相機高度，以及貨架消失點的方向，拿捲尺量的值比對。
跟 app 用同一套程式（drivers/front_camera.FrontCamera、core/store/vision、config.json 的 camera／vision），
不會修改 config.json。

用法：
    python3 -m tools.verify_store_vision                  # 每輪 3 秒
    python3 -m tools.verify_store_vision --seconds 5
    python3 -m tools.verify_store_vision --no-save        # 不存標註影像

每一輪：把推車停好 → Enter → 收 --seconds 秒的影像 → 印出這輪的中位數與 app 會不會採用 →
輸入捲尺量的實際值（Enter 跳過）→ 印誤差。標註影像存到 data/vision_check/<開始時間>/round<k>.png
（偵測到的線段、消失點、量測結果），可以放進報告。

量測量（定義見 core/store/vision.py 開頭與 README「店面平面圖與推車位置」）：
    ℓ_f    標記中心在推車前方幾公尺（相機正下方的地面點量起，沿車頭方向）
    ℓ_r    標記中心在推車右方幾公尺（左邊為負）
    h      由標記算出來的相機離地高度，跟 vision.camera_height_m 比
    d      水平距離 √(ℓ_f² + ℓ_r²)
    δ      消失點方向與光軸的水平夾角（度，右為正）；車頭正對貨架方向時應接近 0
    β      相機俯角 = atan((c_y − v) / f_y)（度），v 是消失點縱座標、c_y 光心縱座標、f_y 縱向焦距（像素）
app 採用條件（ui/workers.py 的 VisionConsumer）：
    標記  d ≤ anchor_trigger_m 且 |h − camera_height_m| ≤ height_tol_m
    消失點 |δ| ≤ vp_max_angle_deg，且至少 vp_min_lines 條線支持

相機開不起來就丟例外結束（不提供沒有相機的替代路徑）。
"""

from __future__ import annotations

import argparse
import math
import statistics
import time
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

from core import config
from core.store import vision as store_vision
from tools._common import ask_float



def vision_config(full_cfg: dict) -> dict:
    """config.json 的 vision 區塊套在 app 的預設值上（跟 app 啟動時一樣）。"""
    return config.vision(full_cfg)


def measure(gray: np.ndarray, detector, K, vcfg: dict) -> Dict:
    """一張去畸變灰階影像的量測結果（標記、消失點），以及 app 會不會採用。"""
    out: Dict = {"anchor": None, "vp": None, "lines": store_vision.detect_lines(gray)}
    s = detector.detect(gray)
    if s is not None:
        out["anchor"] = {
            "forward_m": s.forward_m, "lateral_m": s.lateral_m, "height_m": s.height_m, "ground_m": s.ground_m,
            "accepted": (s.ground_m <= float(vcfg["anchor_trigger_m"])
                         and abs(s.height_m - float(vcfg["camera_height_m"])) <= float(vcfg["height_tol_m"])),
        }
    h, w = gray.shape[:2]
    vp = store_vision.vanishing_point(out["lines"], w, h, min_lines=int(vcfg["vp_min_lines"]))
    if vp is not None:
        delta = store_vision.vp_offset_deg(vp.u, vp.v, K)
        beta = math.degrees(math.atan2(float(K[1][2]) - vp.v, float(K[1][1])))
        out["vp"] = {"u": vp.u, "v": vp.v, "lines": vp.lines, "delta_deg": delta, "beta_deg": beta,
                     "accepted": abs(delta) <= float(vcfg["vp_max_angle_deg"])}
    return out


def summarize(results: List[Dict]) -> Dict:
    """一輪多張影像 → 偵測率與中位數。"""
    n = len(results)
    anchors = [r["anchor"] for r in results if r["anchor"] is not None]
    vps = [r["vp"] for r in results if r["vp"] is not None]

    def med(rows, key):
        return statistics.median(x[key] for x in rows) if rows else None

    return {
        "frames": n,
        "anchor_frames": len(anchors),
        "anchor_accepted": sum(1 for a in anchors if a["accepted"]),
        "forward_m": med(anchors, "forward_m"),
        "lateral_m": med(anchors, "lateral_m"),
        "height_m": med(anchors, "height_m"),
        "ground_m": med(anchors, "ground_m"),
        "vp_frames": len(vps),
        "vp_accepted": sum(1 for v in vps if v["accepted"]),
        "delta_deg": med(vps, "delta_deg"),
        "beta_deg": med(vps, "beta_deg"),
        "vp_lines": med(vps, "lines"),
    }


def _fmt(x: Optional[float], unit: str, nd: int = 3) -> str:
    return "—" if x is None else f"{x:.{nd}f} {unit}"


def _annotate(gray: np.ndarray, r: Dict, summary: Dict) -> np.ndarray:
    import cv2

    img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    for x1, y1, x2, y2 in r["lines"]:
        cv2.line(img, (int(x1), int(y1)), (int(x2), int(y2)), (0, 200, 255), 1)
    h, w = gray.shape[:2]
    if r["vp"] is not None:
        u, v = r["vp"]["u"], r["vp"]["v"]
        if 0 <= u < w and 0 <= v < h:
            cv2.drawMarker(img, (int(u), int(v)), (0, 0, 255), cv2.MARKER_CROSS, 24, 2)
        else:   # 消失點在畫面外（相機往下俯時常見）：在邊緣畫箭頭指過去
            cx, cy = w / 2, h / 2
            k = min(abs((w / 2 - 8) / (u - cx)) if u != cx else 1e9, abs((h / 2 - 8) / (v - cy)) if v != cy else 1e9)
            tip = (int(cx + k * (u - cx)), int(cy + k * (v - cy)))
            cv2.arrowedLine(img, (int(cx), int(cy)), tip, (0, 0, 255), 2, tipLength=0.08)
    text = [f"anchor {summary['anchor_frames']}/{summary['frames']}  fwd {_fmt(summary['forward_m'], 'm', 2)}  "
            f"right {_fmt(summary['lateral_m'], 'm', 2)}  h {_fmt(summary['height_m'], 'm', 2)}",
            f"vp {summary['vp_frames']}/{summary['frames']}  delta {_fmt(summary['delta_deg'], 'deg', 1)}  "
            f"beta {_fmt(summary['beta_deg'], 'deg', 1)}"]
    for i, t in enumerate(text):
        cv2.putText(img, t, (8, 20 + 20 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
        cv2.putText(img, t, (8, 20 + 20 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return img


def _report(s: Dict, vcfg: dict) -> None:
    print(f"  影像 {s['frames']} 張")
    print(f"  定位點標記：看到 {s['anchor_frames']} 張、app 會採用 {s['anchor_accepted']} 張")
    if s["anchor_frames"]:
        print(f"    前方 ℓ_f = {_fmt(s['forward_m'], 'm')}　右方 ℓ_r = {_fmt(s['lateral_m'], 'm')}　"
              f"水平距離 d = {_fmt(s['ground_m'], 'm')}（採用上限 {vcfg['anchor_trigger_m']} m）")
        print(f"    相機高 h = {_fmt(s['height_m'], 'm')}（config {vcfg['camera_height_m']} ± {vcfg['height_tol_m']} m）")
    print(f"  消失點：找到 {s['vp_frames']} 張、app 會採用 {s['vp_accepted']} 張")
    if s["vp_frames"]:
        print(f"    δ = {_fmt(s['delta_deg'], '°', 1)}（採用上限 ±{vcfg['vp_max_angle_deg']}°）　"
              f"β = {_fmt(s['beta_deg'], '°', 1)}　支持線段中位數 {s['vp_lines']:.0f}")


def run(seconds: float, save: bool) -> int:
    from drivers.front_camera import FrontCamera

    full_cfg = config.load()
    vcfg = vision_config(full_cfg)
    camera = FrontCamera(full_cfg.get("camera", {}), proc_width=int(vcfg["proc_width"]))
    try:
        detector = store_vision.AnchorDetector(vcfg["anchor_marker_id"], vcfg["anchor_marker_size_m"], camera.K)
        print(f"前方相機 {camera.size[0]}x{camera.size[1]}，標記 #{vcfg['anchor_marker_id']}、"
              f"邊長 {vcfg['anchor_marker_size_m']} m，相機高 {vcfg['camera_height_m']} m")
        out_dir = config.DATA_DIR / "vision_check" / datetime.now().strftime("%Y%m%d-%H%M%S")
        period = 1.0 / max(0.5, float(vcfg["fps"]))
        k = 0
        while True:
            if input("\n推車停好後按 Enter 開始（q 結束）：").strip().lower() == "q":
                return 0
            k += 1
            results, last_gray = [], None
            t_end = time.time() + seconds
            while time.time() < t_end:
                t0 = time.time()
                last_gray = camera.read_gray()
                results.append(measure(last_gray, detector, camera.K, vcfg))
                time.sleep(max(0.0, period - (time.time() - t0)))
            s = summarize(results)
            _report(s, vcfg)
            if save and last_gray is not None:
                import cv2

                out_dir.mkdir(parents=True, exist_ok=True)
                path = out_dir / f"round{k}.png"
                cv2.imwrite(str(path), _annotate(last_gray, results[-1], s))
                print(f"  標註影像：{config.rel(path)}")
            if s["anchor_frames"]:
                f_true = ask_float("  捲尺量的前方距離 ℓ_f（m，Enter 跳過）：")
                if f_true is not None:
                    print(f"    誤差 {s['forward_m'] - f_true:+.3f} m（{100 * (s['forward_m'] - f_true) / f_true:+.1f}%）")
                h_true = ask_float("  捲尺量的相機鏡頭離地高度（m，Enter 跳過）：")
                if h_true is not None:
                    print(f"    誤差 {s['height_m'] - h_true:+.3f} m")
    finally:
        camera.stop()


def _main() -> int:
    ap = argparse.ArgumentParser(description="前方相機現場驗證（定位點標記、消失點）")
    ap.add_argument("--seconds", type=float, default=3.0, help="每一輪收幾秒影像")
    ap.add_argument("--no-save", action="store_true", help="不存標註影像")
    args = ap.parse_args()
    return run(args.seconds, save=not args.no_save)


if __name__ == "__main__":
    raise SystemExit(_main())
