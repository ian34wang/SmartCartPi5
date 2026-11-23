"""
tests/run_all.py

跑完 tests/ 底下所有測試，最後列出每一支的結果。都不需要硬體。

    python3 tests/run_all.py

test_ui_layout.py 需要 playwright 與 chromium，沒裝的話標成「略過」，不算失敗。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPTIONAL = {"test_ui_layout.py": "playwright"}


def main() -> int:
    results = []
    for path in sorted(HERE.glob("test_*.py")):
        t0 = time.time()
        proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, cwd=HERE.parent)
        out = proc.stdout + proc.stderr
        passed = out.count("  PASS  ") + sum(1 for line in out.splitlines() if line.rstrip().endswith("  OK"))
        failed = out.count("  FAIL  ")
        if proc.returncode != 0 and path.name in OPTIONAL and f"No module named '{OPTIONAL[path.name]}'" in out:
            status = "略過（沒裝 " + OPTIONAL[path.name] + "）"
        elif proc.returncode == 0 and failed == 0:
            status = "通過"
        else:
            status = "失敗"
            print(f"\n===== {path.name} =====\n{out[-3000:]}")
        results.append((path.name, status, passed, failed, time.time() - t0))

    print("\n" + "=" * 64)
    for name, status, passed, failed, sec in results:
        print(f"{name:<34} {status:<14} {passed:>4} 項通過 {failed:>3} 項失敗  {sec:5.1f} s")
    bad = [r for r in results if r[1] == "失敗"]
    print("=" * 64)
    print("全部通過" if not bad else f"{len(bad)} 支失敗")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
