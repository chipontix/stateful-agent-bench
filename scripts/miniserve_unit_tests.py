"""mini-serve's own scheduler and block pool tests (tests/test_scheduler.py, a fake runner, no
GPU) on each state of the miniserve task: what a unit-test grade would have said.
Writes results/raw/miniserve-unit-tests.txt. Run: python3 scripts/miniserve_unit_tests.py"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bench import workspace  # noqa: E402

t = workspace.load("miniserve-preempt-frees-shared-block")
lines = []
for kind in ("bug", "fix", "wrong", "cheat"):
    with tempfile.TemporaryDirectory() as tmp:
        repo = workspace.build(t, Path(tmp) / "r", *([] if kind == "bug" else [t.patch(kind)]))
        r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_scheduler"], cwd=repo,
                           capture_output=True, text=True)
        ran = re.search(r"Ran (\d+) tests?", r.stderr).group(1)
        lines.append(f"{kind:6} {ran} tests: {r.stderr.strip().splitlines()[-1]}")
        print(lines[-1])
(ROOT / "results" / "raw" / "miniserve-unit-tests.txt").write_text(
    f"mini-serve tests/test_scheduler.py at {t.commit[:7]} on each task state\n" + "\n".join(lines) + "\n")
