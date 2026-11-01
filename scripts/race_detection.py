"""How often the crash harness alone (no sabrace) reaches the hold/release
race: ledger-hold-release-race, bug x3 and wrong fix x2, 2 seeds per crash
point. Writes results/raw/race-detection-crash-harness-only.txt.
Run: python3 scripts/race_detection.py"""

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bench import verify, workspace as w  # noqa: E402

os.environ.update(SAB_LEDGER_SEEDS="2", SAB_CHAOS_ONLY="1")
t = w.load("ledger-hold-release-race")
lines = ["# ledger-hold-release-race, crash harness only (SAB_CHAOS_ONLY=1), SAB_LEDGER_SEEDS=2: 8 runs per verification",
         "# columns: state, repetition, verifier pass, [(crash point, seed, run pass)], slowest quiesce s"]
for kind, reps in (("bug", 3), ("wrong", 2)):
    for i in range(reps):
        d = w.ROOT / "work" / "race" / f"{kind}-{i}"
        repo = w.build(t, d / "repo", *([] if kind == "bug" else [t.patch(kind)]))
        res = verify.run(t, repo, d, seed=i + 1)
        runs = json.loads((d / "vrepo" / "chaos.json").read_text())
        lines.append(f"{kind} {i} {res['pass']} {[(r['point'], r['seed'], r['pass']) for r in runs]} "
                     f"{res['metrics'].get('max_quiesce_s')}")
        print(lines[-1], flush=True)
        shutil.rmtree(d)
(w.ROOT / "results" / "raw" / "race-detection-crash-harness-only.txt").write_text("\n".join(lines) + "\n")
