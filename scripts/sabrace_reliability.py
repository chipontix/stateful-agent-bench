"""Is the concurrent ledger stress (verifiers/ledger/sabrace.go) a reliable
grader? Runs it alone N times with different seeds on the reference, the bug
and the wrong fix of both ledger tasks; writes results/raw/sabrace-reliability.json.
Needs the stand's Postgres. Run: python3 scripts/sabrace_reliability.py [N]"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bench import workspace as w  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
pg = os.environ.get("SAB_PG_URL", "postgres://postgres:postgres@localhost:23432/postgres?sslmode=disable")
env = dict(os.environ, GOFLAGS="-mod=mod", GOCACHE=str(ROOT / ".cache" / "go-build"))
out = {"n": N, "states": {}}
for tid in ("ledger-hold-release-race", "ledger-retry-double-charge"):
    t = w.load(tid)
    for kind in ("fix", "bug", "wrong"):
        d = ROOT / "work" / "sabrace" / f"{tid}-{kind}"
        repo = w.build(t, d / "repo", *([] if kind == "bug" else [t.patch(kind)]))
        (repo / "cmd" / "sabrace").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "verifiers" / "ledger" / "sabrace.go", repo / "cmd" / "sabrace" / "main.go")
        subprocess.run(["go", "build", "-o", "sabrace", "./cmd/sabrace"], cwd=repo, env=env, check=True)
        runs = []
        for seed in range(1, N + 1):
            subprocess.run(["./sabrace", "-admin", pg, "-seed", str(seed), "-out", "r.json"], cwd=repo,
                           capture_output=True)
            r = json.loads((repo / "r.json").read_text())
            runs.append({"seed": seed, "pass": r["pass"], "first_problem": (r["problems"] or [""])[0][:160],
                         "commands_per_s": round(r["metrics"].get("commands_per_s", 0))})
        row = {"passed": sum(x["pass"] for x in runs), "runs": runs}
        out["states"][f"{tid}/{kind}"] = row
        print(f"{tid:28} {kind:5} passed {row['passed']}/{N}", flush=True)
        shutil.rmtree(d)
(ROOT / "results" / "raw" / "sabrace-reliability.json").write_text(json.dumps(out, indent=2) + "\n")
