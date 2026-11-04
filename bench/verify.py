"""Runs a task's verifier on a repository and returns its result."""

import json
import os
import subprocess
import time
from pathlib import Path

from . import workspace

ROOT = workspace.ROOT


def verifier_broke(problems: list[str]) -> bool:
    """Only when the verifier's own error comes first: a verifier that trips over
    what broken code left behind (no tables, no output) still graded that code."""
    return bool(problems) and problems[0].startswith(("verifier error", "verifier exited"))


def run(task: workspace.Task, repo: Path, out_dir: Path, seed: int = 1) -> dict:
    """Verifies repo (left untouched) in out_dir/vrepo: protected paths are put
    back first. Result: pass, problems, metrics, seconds, tampered."""
    out_dir = out_dir.resolve()
    repo = repo.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    vrepo = out_dir / "vrepo"
    tampered = workspace.verification_copy(task, repo, vrepo)
    out = out_dir / "result.json"
    out.unlink(missing_ok=True)
    v = task.verifier
    cmd = [str(ROOT / "verifiers" / v["kind"] / "run.sh"), str(vrepo), str(out)]
    if v["kind"] == "ledger":
        cmd.append(v["points"])
    env = dict(os.environ, SAB_SEED=str(seed), SAB_COMMIT=task.commit, GOFLAGS="-mod=mod",
               GOCACHE=os.environ.get("GOCACHE", str(ROOT / ".cache" / "go-build")))
    t0 = time.monotonic()
    with open(out_dir / "verifier.log", "w") as log:
        proc = subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
    secs = time.monotonic() - t0
    if out.exists():
        res = json.loads(out.read_text())
    else:
        res = {"pass": False, "problems": [f"verifier exited {proc.returncode} without a result"], "metrics": {}}
    res["problems"] = res.get("problems") or []
    res.update(seconds=round(secs, 1), tampered=tampered)
    res.setdefault("stage", "invariants")
    if verifier_broke(res["problems"]):
        res["stage"] = "verifier-error"  # the verifier broke: this says nothing about the code under test
    if proc.returncode != 0 and res["pass"]:  # never trust a pass the process did not confirm
        res["pass"] = False
        res["problems"].append(f"verifier exit code {proc.returncode}")
    return res
