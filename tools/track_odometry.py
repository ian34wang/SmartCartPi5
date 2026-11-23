"""
tools/track_odometry.py

里程計（PMW3901 光流 + BNO080 航向）行駛軌跡視覺化：推車的同時在螢幕上即時畫出軌跡，推完輸入捲尺量的
終點與實際轉角，算誤差、存圖。位置的算法就是 app 的 core/positioning/odometry.OdometryEngine，參數讀
config.json 的 odometry 區塊（tools/calibrate_optical_flow.py 寫回的），不會修改 config.json。

用法：
    python3 -m tools.track_odometry                       # 即時視窗（要在 Pi 的桌面上跑）
    python3 -m tools.track_odometry --grid-m 0.3          # 格線 0.3 m，對照地上的格子
    python3 -m tools.track_odometry --no-window           # 沒有桌面（SSH）：終端機顯示，結束後一樣存圖
    python3 -m tools.track_odometry --from-data data/odometry_tracks/<時間>      # 用錄音重算、重畫（不用硬體）
    python3 -m tools.track_odometry --from-data data/flow_calibration/<時間>     # 光流校正錄音也可以

每一趟：推車停在起點、車頭擺好 → Enter → 推（直線、L 形、繞一圈回起點都可以）→ 停好按 Enter →
輸入終點在起點的「前方, 左方」幾公尺（起點車頭方向為前方；繞回起點輸入 0,0）與實際轉了幾度（左轉為正）。
可以一直測下一趟，輸入 q 結束。存檔：data/odometry_tracks/<開始時間>/
    samples.csv、samples.meta.json   每筆原始封包、每趟的實際值（core/recording.py 格式，可以 --from-data 重算）
    run<k>.png                       軌跡圖（起點朝上）
    result.json                      每趟的指標與當時的 odometry 參數

圖與指標的座標：原點是起點、上 = 起點時的車頭方向（f）、左 = 起點時的左方（l），公尺。
指標定義見 core/positioning/track.py：
    L    路徑長          D    起點到終點直線距離        Δψ   總轉角（左轉為正）
    ‖e‖  終點誤差 = 算出的終點 (f_N, l_N) 跟捲尺量的 (f*, l*) 的距離；‖e‖/L 每走一公尺差多少
    e_ψ  轉角誤差 = Δψ − 實際轉角

UART 接不上就報錯結束（不提供沒有硬體的替代路徑）。app 開著的時候序列埠被佔用，先關 app。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.positioning import track as ot
from tools._common import parse_float, parse_pair
from core import config
from core.recording import Segment, UartTap, load, new_session_dir, save_result, wait_for_stream

_OUT_ROOT = config.DATA_DIR / "odometry_tracks"
_WINDOW = "SmartCart odometry track"




def print_metrics(m: Dict) -> None:
    if "path_m" not in m:
        print("    沒有資料（這一趟沒收到封包）")
        return
    print(f"    路徑長 L = {m['path_m']:.3f} m　　直線距離 D = {m['displacement_m']:.3f} m　　"
          f"總轉角 Δψ = {m['turn_deg']:+.1f}°　　時間 {m['duration_s']:.1f} s")
    print(f"    終點（起點座標）f_N = {m['end_f_m']:+.3f} m（前方）　l_N = {m['end_l_m']:+.3f} m（左方）")
    print(f"    squal 中位數 {m['squal_median']:.0f}、最低 5% {m['squal_p5']}　　"
          f"掉資料 {m['gaps']} 次（最長 {m['max_gap_s']:.2f} s）　　低 squal 跳過 {m['skipped_low_squal']} 筆")
    if m.get("end_err_m") is not None:
        pct = m["end_err_pct_of_path"]
        print(f"    終點誤差 ‖e‖ = {m['end_err_m']:.3f} m（前方 {m['end_err_f_m']:+.3f}、左方 {m['end_err_l_m']:+.3f}）"
              + (f"，‖e‖/L = {pct:.1f}%" if pct is not None else ""))
    if m.get("distance_err_m") is not None:
        print(f"    直線距離誤差 e_D = {m['distance_err_m']:+.3f} m")
    if m.get("turn_err_deg") is not None:
        print(f"    轉角誤差 e_ψ = {m['turn_err_deg']:+.1f}°")


# ----------------------------------------------------------------------
# 即時
# ----------------------------------------------------------------------
class LiveView:
    """主執行緒畫圖、輸入執行緒問問題，共用這份狀態。"""

    def __init__(self, odo_cfg: dict, min_squal: int, grid_m: Optional[float], size: Tuple[int, int]):
        self.odo_cfg, self.min_squal, self.grid_m, self.size = odo_cfg, min_squal, grid_m, size
        self._lock = threading.Lock()
        self.label: Optional[str] = None       # 正在錄的那一趟
        self.builder: Optional[ot.TrackBuilder] = None
        self._n = 0
        self.frozen = None                      # 上一趟的結果圖
        self.message = "press ENTER in the terminal to start run 1"

    def start(self, label: str) -> None:
        with self._lock:
            self.label, self.builder, self._n = label, ot.TrackBuilder(self.odo_cfg, self.min_squal), 0

    def poll(self, tap: UartTap) -> None:
        with self._lock:
            if self.label is None:
                return
            new = tap.since(self.label, self._n)
            self._n += len(new)
            self.builder.feed_all(new)

    def stop(self, tap: UartTap) -> ot.Track:
        self.poll(tap)
        with self._lock:
            self.label = None
            return self.builder.track

    def live_metrics(self) -> Optional[Dict]:
        with self._lock:
            if self.label is None or self.builder is None:
                return None
            return ot.metrics(self.builder.track)

    def image(self):
        with self._lock:
            if self.label is not None and self.builder is not None:
                tr = self.builder.track
                m = ot.metrics(tr)
                return ot.render(tr.start_frame(), self.size, self.grid_m,
                                 lines=[f"{self.label} (recording)  press ENTER in the terminal to stop"]
                                 + ot.summary_lines(self.label, m)[:1],
                                 heading_deg=m.get("turn_deg"))
            if self.frozen is not None:
                return self.frozen
            return ot.render([], self.size, self.grid_m, lines=[self.message])


def _controller(tap: UartTap, view: LiveView, out_dir: Path, results: List[Dict], done: threading.Event) -> None:
    try:
        k = 0
        while True:
            k += 1
            label = f"run{k}"
            cmd = input(f"\n[第 {k} 趟] 推車停在起點、車頭擺好，按 Enter 開始（q 結束）：").strip().lower()
            if cmd == "q":
                return
            view.start(label)
            tap.begin(label)
            input("    錄音中，推到終點停好後按 Enter：")
            seg: Segment = tap.end()
            track = view.stop(tap)
            print()
            print_metrics(ot.metrics(track))

            pair = parse_pair(input("    終點在起點的「前方, 左方」幾公尺（捲尺量；繞回起點輸入 0,0；Enter 跳過）："))
            turn = parse_float(input("    實際轉了幾度（左轉為正，例如 90、-90、360；Enter 跳過）："))
            if pair is not None:
                seg.info.update(true_f_m=pair[0], true_l_m=pair[1])
            if turn is not None:
                seg.info["true_turn_deg"] = turn
            tap.write_meta()
            m = ot.metrics(track, **ot.truth_from_info(seg.info))
            if pair is not None or turn is not None:
                print_metrics(m)
            img = ot.render(track.start_frame(), view.size, view.grid_m, lines=ot.summary_lines(label, m),
                            truth_end=pair, heading_deg=m.get("turn_deg"))
            _save_png(out_dir / f"{label}.png", img)
            results.append({"label": label, **seg.info, **m})
            view.frozen = img
            view.message = f"press ENTER in the terminal to start run {k + 1}"
    finally:
        done.set()


def _save_png(path: Path, img) -> None:
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)
    print(f"    軌跡圖：{config.rel(path)}")


def run_live(cfg: dict, args) -> int:
    from drivers.uart_receiver import UartReceiver

    window = not args.no_window
    if window and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("[錯誤] 沒有圖形畫面（DISPLAY、WAYLAND_DISPLAY 都沒設定），開不了即時視窗。")
        return 1
    odo_cfg = cfg.get("odometry", {})
    print(f"odometry 參數：px_to_mm = {odo_cfg.get('optical_flow_px_to_mm')}、flow_rotation_deg = "
          f"{odo_cfg.get('flow_rotation_deg')}、flow_mirror = {odo_cfg.get('flow_mirror')}、yaw_sign = {odo_cfg.get('yaw_sign')}")

    receiver = UartReceiver.from_config(cfg, port=args.port, baudrate=args.baud)
    receiver.start()
    tap = None
    results: List[Dict] = []
    out_dir = new_session_dir(_OUT_ROOT)
    try:
        if not wait_for_stream(receiver):
            print(f"[錯誤] 5 秒內沒有收到任何 UART 封包（{args.port}）。")
            return 1
        tap = UartTap(receiver, out_dir / "samples.csv", kind="odometry_track",
                      extra_meta={"odometry": odo_cfg, "min_squal": args.min_squal})
        tap.start()
        view = LiveView(odo_cfg, args.min_squal, args.grid_m, (args.size, args.size))
        done = threading.Event()
        th = threading.Thread(target=_controller, args=(tap, view, out_dir, results, done), daemon=True)
        th.start()
        if window:
            import cv2

            cv2.namedWindow(_WINDOW, cv2.WINDOW_AUTOSIZE)
        while not done.is_set():
            view.poll(tap)
            if window:
                cv2.imshow(_WINDOW, view.image())
                cv2.waitKey(100)
            else:
                m = view.live_metrics()
                if m is not None and "path_m" in m:
                    print(f"\r    L = {m['path_m']:6.2f} m  f = {m['end_f_m']:+6.2f}  l = {m['end_l_m']:+6.2f}  "
                          f"Δψ = {m['turn_deg']:+6.1f}°  squal = {m['squal_median']:.0f}   ", end="", flush=True)
                time.sleep(0.2)
        if window:
            cv2.destroyAllWindows()
    finally:
        if tap is not None:
            tap.stop()
        receiver.stop()
    if results:
        save_result(out_dir, {"odometry": odo_cfg, "min_squal": args.min_squal, "runs": results})
        print(f"\n存在 {config.rel(out_dir)}（{len(results)} 趟）")
    return 0


# ----------------------------------------------------------------------
# 重播
# ----------------------------------------------------------------------
def run_replay(cfg: dict, args) -> int:
    path = Path(args.from_data)
    out_dir = path if path.is_dir() else path.parent
    rec = load(path)
    odo_cfg = cfg.get("odometry", {})
    print(f"讀入 {config.rel(out_dir)}：{len(rec.segments)} 段，用目前 config.json 的 odometry 參數重算")
    results = []
    for seg in rec.segments:
        samples = rec.get(seg.label)
        if len(samples) < 2:
            continue
        track = ot.build_track(samples, odo_cfg, args.min_squal)
        truth = ot.truth_from_info(seg.info)
        m = ot.metrics(track, **truth)
        print(f"\n[{seg.label}]" + (f"  實際值 {truth}" if truth else ""))
        print_metrics(m)
        end = (truth["true_f_m"], truth["true_l_m"]) if "true_f_m" in truth else None
        img = ot.render(track.start_frame(), (args.size, args.size), args.grid_m,
                        lines=ot.summary_lines(seg.label, m), truth_end=end, heading_deg=m.get("turn_deg"))
        _save_png(out_dir / f"track_{seg.label}.png", img)
        results.append({"label": seg.label, **seg.info, **m})
    (out_dir / "track_result.json").write_text(
        json.dumps({"odometry": odo_cfg, "min_squal": args.min_squal, "runs": results}, ensure_ascii=False, indent=2)
        + "\n", encoding="utf-8")
    return 0


def _main() -> int:
    cfg = config.load()
    ap = argparse.ArgumentParser(description="里程計行駛軌跡視覺化（光流 + IMU）")
    ap.add_argument("--from-data", default=None, help="用存下來的錄音重算、重畫（目錄或 samples.csv）")
    ap.add_argument("--no-window", action="store_true", help="不開即時視窗，終端機顯示")
    ap.add_argument("--grid-m", type=float, default=None, help="格線間距（m），預設依範圍自動")
    ap.add_argument("--size", type=int, default=720, help="圖的邊長（像素）")
    ap.add_argument("--min-squal", type=int, default=0, help="squal 低於這個不累加位置（app 是 0）")
    ap.add_argument("--port", default=cfg.get("serial", {}).get("port", "/dev/ttyAMA0"))
    ap.add_argument("--baud", type=int, default=cfg.get("serial", {}).get("baudrate", 115200))
    args = ap.parse_args()
    if args.from_data:
        return run_replay(cfg, args)
    return run_live(cfg, args)


if __name__ == "__main__":
    sys.exit(_main())
