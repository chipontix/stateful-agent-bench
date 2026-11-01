"""Merges sabrace's race.json and the crash harness's chaos.json into the
bench result.

Budgets: sabrace handles at least its -budget commands/s; every crash run
quiesces (all sagas terminal, outboxes drained) within SAB_BUDGET_QUIESCE_S
seconds of the workload's end."""

import json
import os
import sys
from pathlib import Path

BUDGET = float(os.environ.get("SAB_BUDGET_QUIESCE_S", 90))
args = sys.argv[1:]
if args[0] == "--build-failed":
    log = Path(args[1]).read_text()[-2000:]
    res = {"pass": False, "stage": "build", "problems": ["build failed: " + log], "metrics": {}}
else:
    race_f, src, out = Path(args[0]), Path(args[1]), args[2]
    race = json.loads(race_f.read_text()) if race_f.exists() else {"problems": ["sabrace produced no result (see race.log)"]}
    problems = ["concurrent: " + p for p in race.get("problems") or []]
    runs = json.loads(src.read_text()) if src.exists() else []
    if not runs:
        problems.append("crash harness produced no runs (see chaos.log)")
    states: dict = {}
    for r in runs:
        for p in r["problems"] or []:
            problems.append(f"{r['point']} seed {r['seed']}: {p}")
        for k, v in (r["states"] or {}).items():
            states[k] = states.get(k, 0) + v
    quiesce = max((r["quiesce_ns"] / 1e9 for r in runs), default=0)
    if runs and not problems and quiesce > BUDGET:
        problems.append(f"performance budget: slowest run quiesced in {quiesce:.1f}s, budget {BUDGET:.0f}s")
    res = {"pass": not problems, "problems": problems, "metrics": {
        "concurrent": race.get("metrics", {}), "runs": len(runs), "passed_runs": sum(r["pass"] for r in runs), "transfers": sum(r["keys"] for r in runs),
        "kills": sum(r["kills"] for r in runs), "states": states, "max_quiesce_s": round(quiesce, 1),
        "budget_quiesce_s": BUDGET}}
Path(args[-1]).write_text(json.dumps(res, indent=2))
print(json.dumps({"pass": res["pass"], "problems": res["problems"][:3]})[:600])
sys.exit(0 if res["pass"] else 1)
