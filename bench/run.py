"""Agent runs: one attempt = fresh workspace, agent in a container without
network, verifier on the result. Everything lands in one JSONL trace."""

import json
import math
import random
import shutil
import subprocess
import time
from pathlib import Path

from . import bridge, mutate, verify, workspace

ROOT = workspace.ROOT
IMAGE = "python:3.13-slim"  # both agents need only Python; already on most Docker hosts
AGENTS = {
    # stubs: how the runner picks the patch the stub plays back
    "stub-ref": lambda rng: "fix",
    "stub-wrong": lambda rng: "wrong",
    "stub-cheat": lambda rng: "cheat",
    "stub-coin": lambda rng: "fix" if rng.random() < 0.5 else "wrong",
    # a real agent loop on a local model (agents/llm.py, bench/bridge.py)
    "llm-qwen-coder-1.5b": None,
    # the same agent and model, told which files the reference fix changes
    "llm-qwen-coder-1.5b-hint": None,
}
HINTED = {"llm-qwen-coder-1.5b-hint"}


def hint_files(task: workspace.Task) -> list[str]:
    """The files the reference fix changes, in patch order."""
    return list(dict.fromkeys(p for p, *_ in mutate.hunks(task.patch("fix").read_text())))


class Trace:
    def __init__(self, path: Path):
        self.path, self.step = path, 0
        path.write_text("")

    @classmethod
    def resume(cls, path: Path) -> "Trace":
        t = cls.__new__(cls)
        t.path = path
        t.step = sum(1 for l in path.read_text().splitlines() if l.strip())
        return t

    def emit(self, actor: str, type_: str, **kw) -> None:
        self.step += 1
        with self.path.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "step": self.step, "actor": actor, "type": type_, **kw}) + "\n")

    def merge(self, agent_trace: Path) -> None:
        for line in agent_trace.read_text().splitlines():
            e = json.loads(line)
            self.step += 1
            e["step"] = self.step
            with self.path.open("a") as f:
                f.write(json.dumps(e) + "\n")


def attempt(task: workspace.Task, agent: str, i: int, out: Path, seed: int, timeout: int = 900) -> None:
    """The agent half: fresh workspace, agent in the container, diff into the
    trace. The workspace stays in `out` for verify_attempt."""
    if out.exists():
        shutil.rmtree(out)
    (out / "task").mkdir(parents=True)
    (out / "io").mkdir()
    tr = Trace(out / "trace.jsonl")
    repo = workspace.build(task, out / "repo")
    shutil.copy(task.dir / "issue.md", out / "task" / "issue.md")
    pick = AGENTS[agent]
    played = None
    if pick:
        played = pick(random.Random(f"{task.id}/{agent}/{seed}/{i}"))
        shutil.copy(task.patch(played), out / "task" / "solution.patch")
        argv = ["python3", "/agents/stub.py", "--trace", "/trace/agent.jsonl"]
    else:
        argv = ["python3", "/agents/llm.py", "--trace", "/trace/agent.jsonl", "--seed", str(seed)]
    hint = hint_files(task) if agent in HINTED else None
    if hint:
        (out / "task" / "hint.txt").write_text("\n".join(hint) + "\n")
        argv += ["--hint", "/task/hint.txt"]
    (out / "agent.jsonl").write_text("")
    name = f"sab-agent-{task.id[:20]}-{i}-{int(time.time())}"
    cmd = ["docker", "run", "--rm", "--name", name, "--network", "none", "--memory", "512m", "--cpus", "1",
           "-v", f"{repo}:/workspace", "-v", f"{out / 'task'}:/task:ro", "-v", f"{ROOT / 'agents'}:/agents:ro",
           "-v", f"{out}:/trace", "-v", f"{out / 'io'}:/io", IMAGE, *argv]
    tr.emit("runner", "start", task=task.id, project=task.project, commit=task.commit, agent=agent, attempt=i,
            image=IMAGE, network="none", played=played, hint=hint)
    t0 = time.monotonic()
    with bridge.Bridge(out / "io") as br:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            rc, err = p.returncode, p.stderr[-1000:]
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            rc, err = -1, f"agent timed out after {timeout}s"
    tr.merge(out / "agent.jsonl")
    tr.emit("runner", "agent_exit", exit=rc, seconds=round(time.monotonic() - t0, 1), stderr=err,
            model_calls=br.calls)
    diff = workspace.changes(repo)
    (out / "agent.diff").write_text(diff)
    paths = workspace.changed_paths(repo)
    tr.emit("runner", "diff", files=paths, added=sum(l.startswith("+") and not l.startswith("+++") for l in diff.splitlines()),
            removed=sum(l.startswith("-") and not l.startswith("---") for l in diff.splitlines()),
            protected=[p for p in paths if workspace.is_protected(task, p)])
    shutil.rmtree(out / "io", ignore_errors=True)
    (out / "played").write_text(played or "")


def verify_attempt(task: workspace.Task, agent: str, i: int, out: Path, seed: int) -> dict:
    """The verifier half, on the workspace attempt() left in `out`."""
    tr = Trace.resume(out / "trace.jsonl")
    if not (out / "agent.diff").read_text().strip():
        # Nothing changed: the repository is the buggy snapshot, which verify-all shows red every time.
        res = {"pass": False, "stage": "no-change", "seconds": 0, "tampered": [], "metrics": {},
               "problems": ["no change: the repository is still the buggy snapshot (red in verify-all)"]}
    else:
        res = verify.run(task, out / "repo", out / "verify", seed=seed)
    tr.emit("verifier", "result", **{k: res[k] for k in ("pass", "stage", "problems", "metrics", "seconds", "tampered")})
    shutil.rmtree(out / "verify" / "vrepo", ignore_errors=True)
    return {"task": task.id, "agent": agent, "attempt": i, "played": (out / "played").read_text() or None,
            "pass": res["pass"], "seconds": res["seconds"], "trace": str((out / "trace.jsonl").relative_to(ROOT))}


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased estimator (Chen et al. 2021): 1 - C(n-c, k) / C(n, k)."""
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)
