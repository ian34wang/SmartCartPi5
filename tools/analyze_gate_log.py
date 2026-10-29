"""
tools/analyze_gate_log.py

分析門口記錄檔（core/gate_recorder.py 寫的，執行期每次啟動都會自動產生一份在
data/gate_logs/<日期>/）。用的是跟執行期同一套判定規則（core/gate_model.py）。

會印出：
    1. 概況：記錄多久、每顆每秒幾包、斷訊（超過 1 秒沒收到）幾次、多少時間在判定
    2. 當時判定出的進出事件（含被航向擋下的）
    3. 一致性：用記錄檔裡當時的參數重播，事件是不是跟當時即時判定的一模一樣
       ——一樣才代表「事後分析看到的」就是「當時發生的」
    4. 差分值分布、各側停留時間
    5. 有給 --set 時：換一組參數重播，看事件會怎麼變（不用再去現場試）

用法：
    python3 -m tools.analyze_gate_log                         # 分析最新一份
    python3 -m tools.analyze_gate_log data/gate_logs/20261029/143012_app.csv
    python3 -m tools.analyze_gate_log --set hysteresis_dbm=6 --set smoothing_sec=1.5
    python3 -m tools.analyze_gate_log --events                # 列出每個事件前後的差分值

    --set 可以改 GateParams 的任一欄位：smoothing_sec、estimator、min_samples、
    hysteresis_dbm、min_crossing_rssi_dbm（none 代表不設）、min_confirm_samples、
    offset_inside（門內偏移量）。

校正量測的資料（data/gate_calibration/）請用
    python3 -m tools.calibrate_gate_beacons --from-data <目錄>
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.gate_model import GateParams, replay  # noqa: E402
from core.gate_recorder import RUNTIME_LOG_ROOT, load  # noqa: E402

_SIDE = {"a": "門內", "b": "門外", None: "未確認", "in": "門內", "out": "門外", "": "未確認"}


def latest_log() -> Optional[Path]:
    logs = sorted(RUNTIME_LOG_ROOT.glob("*/*.csv"))
    return logs[-1] if logs else None


def apply_overrides(p: GateParams, sets: List[str]) -> GateParams:
    for item in sets:
        key, _, val = item.partition("=")
        key, val = key.strip(), val.strip()
        if key == "offset_inside":
            p = p.with_offset_inside(float(val))
            continue
        if key not in GateParams.__dataclass_fields__:
            raise SystemExit(f"[錯誤] 不認得的參數 {key!r}")
        cur = getattr(p, key)
        if val.lower() == "none":
            new = None
        elif isinstance(cur, bool):
            new = val.lower() in ("1", "true", "yes")
        elif isinstance(cur, int):
            new = int(val)
        elif isinstance(cur, float) or cur is None:
            new = float(val)
        else:
            new = val
        p = replace(p, **{key: new})
    return p


def _gaps(times: List[float], limit: float = 1.0) -> int:
    return sum(1 for a, b in zip(times, times[1:]) if b - a > limit)


def _pct(vals: List[float], q: float) -> float:
    s = sorted(vals)
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def main() -> int:
    ap = argparse.ArgumentParser(description="分析門口記錄檔")
    ap.add_argument("path", nargs="?", help="記錄檔 .csv（不給就用最新一份）")
    ap.add_argument("--set", action="append", default=[], help="換參數重播，例如 hysteresis_dbm=6")
    ap.add_argument("--events", action="store_true", help="列出每個事件前後 3 秒的差分值")
    args = ap.parse_args()

    path = Path(args.path) if args.path else latest_log()
    if path is None or not path.exists():
        print(f"[錯誤] 找不到記錄檔（{path or RUNTIME_LOG_ROOT}）")
        return 1
    segments, live, meta = load(path)
    if set(segments) - {"run"}:
        print("這是校正量測的資料，請用：python3 -m tools.calibrate_gate_beacons --from-data "
              f"{path.parent}")
        return 1
    if "params" not in meta:
        print(f"[錯誤] 找不到 {path.with_suffix('.meta.json')}，不知道當時用的參數")
        return 1
    obs = segments.get("run", [])
    rows = live.get("run", [])
    if not obs:
        print("記錄檔是空的。")
        return 1
    params = GateParams.from_dict(meta["params"])
    t0 = obs[0][0]

    # 1. 概況 --------------------------------------------------------------
    dur = obs[-1][0] - t0
    print(f"記錄檔：{path}")
    print(f"開始時間：{meta.get('started_at', '?')}　長度 {dur / 60:.1f} 分鐘　共 {len(obs)} 筆")
    for bid in (params.inside_id, params.outside_id):
        ts = [o[0] for o in obs if o[1] == bid]
        rate = len(ts) / dur if dur > 0 else 0
        print(f"  {bid:<14} {rate:4.1f} 包/秒　斷訊 >1 秒 {_gaps(ts)} 次")
    judged = [r for r in rows if r["diff"]]
    print(f"  有判定的比例 {len(judged) / len(rows) * 100:.0f}%（其餘是窗內樣本不夠）")
    print(f"  當時參數：窗口 {params.smoothing_sec:g} 秒 / {params.estimator} / 遲滯 {params.hysteresis_dbm:g} dB / "
          f"門內偏移 {params.offset_inside:+.1f} / 絕對門檻 {params.min_crossing_rssi_dbm}")

    # 2. 當時的事件 ----------------------------------------------------------
    live_events = [(float(r["epoch"]), r["event"]) for r in rows if r["event"]]
    print(f"\n當時判定出的事件（{len(live_events)} 個）：")
    for t, e in live_events:
        print(f"  {t - t0:8.1f} 秒　{e}")

    # 3. 一致性 --------------------------------------------------------------
    rep = replay(obs, params)
    live_kept = [(round(t, 3), e) for t, e in live_events if not e.startswith("rejected_")]
    rep_kept = [(round(t, 3), e) for t, e in rep.events]
    same = live_kept == rep_kept
    print(f"\n一致性：用當時參數重播 → {len(rep_kept)} 個事件，"
          f"{'✓ 跟當時即時判定完全一樣' if same else '⚠ 跟當時不一樣（程式或資料有出入）'}")
    if not same:
        print(f"  當時：{live_kept}\n  重播：{rep_kept}")

    # 4. 差分分布 ------------------------------------------------------------
    diffs = [s.diff for s in rep.steps]
    if diffs:
        print("\n差分值（已加偏移）分布："
              + "　".join(f"p{int(q * 100)} {_pct(diffs, q):+.1f}" for q in (0.05, 0.25, 0.5, 0.75, 0.95)))
        share = {"a": 0, "b": 0, None: 0}
        for s in rep.steps:
            share[s.dominant] += 1
        print("各側比例：" + "　".join(f"{_SIDE[k]} {v / len(rep.steps) * 100:.0f}%" for k, v in share.items()))

    if args.events and rep.events:
        print("\n每個事件前後 3 秒的差分值（每 0.5 秒取一筆）：")
        for t, e in rep.events:
            near = [s for s in rep.steps if t - 3 <= s.t <= t + 3]
            picked, last = [], None
            for s in near:
                if last is None or s.t - last >= 0.5:
                    picked.append(s)
                    last = s.t
            print(f"  {t - t0:8.1f} 秒 {e}：" + " ".join(f"{s.diff:+.0f}" for s in picked))

    # 5. 換參數 --------------------------------------------------------------
    if args.set:
        alt = apply_overrides(params, args.set)
        r2 = replay(obs, alt)
        print(f"\n換參數重播（{', '.join(args.set)}）→ {len(r2.events)} 個事件：")
        for t, e in r2.events:
            print(f"  {t - t0:8.1f} 秒　{e}")
        d2 = [s.diff for s in r2.steps]
        if d2:
            print(f"  有判定的比例 {r2.judged / max(r2.evaluations, 1) * 100:.0f}%　"
                  f"差分中位數 {statistics.median(d2):+.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
