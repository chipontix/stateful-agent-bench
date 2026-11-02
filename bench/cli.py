"""python3 -m bench <command>; see README "Commands"."""

import argparse
import contextlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import audit, bridge, mutate, run, verify, workspace

ROOT = workspace.ROOT
WORK = ROOT / "work"
RESULTS = ROOT / "results"


def state(task: workspace.Task, kind: str, dest: Path) -> Path:
    """bug | fix | wrong | cheat: the buggy snapshot plus that patch."""
    extra = [] if kind == "bug" else [task.patch(kind)]
    return workspace.build(task, dest, *extra)


def tasks(names: list[str]) -> list[workspace.Task]:
    return [workspace.load(n) for n in names] if names else workspace.all_tasks()


def host() -> dict:
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "machine": platform.machine(),
            "system": platform.system(), "cpus": os.cpu_count(),
            "load_average": " ".join(f"{x:.2f}" for x in os.getloadavg())}


def save(name: str, data: dict) -> None:
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")


def checked(task, kind, d, seed) -> dict:
    repo = state(task, kind, d / "repo")
    res = verify.run(task, repo, d, seed=seed)
    shutil.rmtree(d, ignore_errors=True)
    return res


def short(res: dict) -> dict:
    return {"pass": res["pass"], "stage": res["stage"], "seconds": res["seconds"], "tampered": res["tampered"],
            "problems": res["problems"][:5], "metrics": res.get("metrics", {})}


def cmd_check(a) -> int:
    task = workspace.load(a.task)
    res = checked(task, a.state, WORK / "check" / f"{task.id}-{a.state}", a.seed)
    print(json.dumps(short(res), indent=1)[:4000])
    return 0 if res["pass"] else 1


def cmd_verify_all(a) -> int:
    """Per task: bug red, reference green, reference N more times green."""
    path = RESULTS / "verify-all.json"
    out = json.loads(path.read_text()) if path.exists() and a.tasks else {"tasks": {}}
    out.update(host=host(), repeat=a.repeat)
    ok = True
    for t in tasks(a.tasks):
        d = WORK / "verify-all" / t.id
        bug = checked(t, "bug", d / "bug", 1)
        refs = [checked(t, "fix", d / f"fix-{s}", s) for s in range(1, a.repeat + 1)]
        row = {"bug": short(bug), "reference": [short(r) for r in refs],
               "bug_red": not bug["pass"] and bug["stage"] != "verifier-error",
               "reference_green": sum(r["pass"] for r in refs)}
        row["ok"] = row["bug_red"] and row["reference_green"] == a.repeat
        ok &= row["ok"]
        row["host"] = host()  # per task: a run of named tasks keeps the other rows and their hosts
        out["tasks"][t.id] = row
        print(f"{t.id:36} bug {'red' if row['bug_red'] else 'GREEN'}  reference {row['reference_green']}/{a.repeat} green"
              f"  ({bug['seconds']:.0f}s + {sum(r['seconds'] for r in refs):.0f}s)", flush=True)
        save("verify-all", out)
    out["ok"] = all(v["ok"] for v in out["tasks"].values())
    save("verify-all", out)
    return 0 if ok else 1


def cmd_run(a) -> int:
    """--phase agent needs no brokers (only Docker for the container and, for
    the llm agent, the local model); --phase verify needs the stand."""
    k = a.k
    runs_dir = WORK / "runs" / a.agent
    attempts = [(t, i) for t in tasks(a.tasks) for i in range(1, a.n + 1)]
    if a.phase in ("all", "agent"):
        model = run.AGENTS[a.agent] is None
        with (bridge.Server(runs_dir / "llama-server.log") if model else contextlib.nullcontext()):
            for t, i in attempts:
                run.attempt(t, a.agent, i, runs_dir / f"{t.id}-{i}", seed=i)
                diff = (runs_dir / f"{t.id}-{i}" / "agent.diff").read_text()
                print(f"{t.id:36} #{i} agent done, diff {len(diff.splitlines())} lines", flush=True)
    if a.phase == "agent":
        return 0
    path = RESULTS / f"run-{a.agent}.json"
    out = {"tasks": {}}
    if path.exists() and a.tasks:  # named tasks: replace their rows, keep the others
        out = json.loads(path.read_text())
        if out.get("attempts_per_task") != a.n or out.get("k") != k:
            sys.exit(f"{path} has n={out.get('attempts_per_task')} k={out.get('k')}; rerun all tasks to change them")
    out.update(host=host(), agent=a.agent, k=k, attempts_per_task=a.n)
    tdir = RESULTS / "traces" / a.agent
    if a.tasks:
        for t in tasks(a.tasks):
            for f in tdir.glob(f"{t.id}-*"):
                f.unlink()
    else:
        shutil.rmtree(tdir, ignore_errors=True)
    tdir.mkdir(parents=True, exist_ok=True)
    by_task: dict = {}
    for t, i in attempts:
        r = run.verify_attempt(t, a.agent, i, runs_dir / f"{t.id}-{i}", seed=i)
        src = ROOT / r["trace"]
        dst = tdir / f"{t.id}-{i}.jsonl"
        shutil.copy(src, dst)
        shutil.copy(src.parent / "agent.diff", tdir / f"{t.id}-{i}.diff")
        r["trace"] = str(dst.relative_to(ROOT))
        rep = audit.audit(audit.load(dst), audit.diff_for(dst))
        (tdir / f"{t.id}-{i}.audit.md").write_text(audit.render(rep))
        r["verdict"] = rep["verdict"]
        by_task.setdefault(t.id, []).append(r)
        print(f"{t.id:36} #{i} played {r['played'] or '-':5} -> {'pass' if r['pass'] else 'fail'}  {rep['verdict']}",
              flush=True)
        shutil.rmtree(runs_dir / f"{t.id}-{i}", ignore_errors=True)
    for tid, rows in by_task.items():
        c = sum(r["pass"] for r in rows)
        out["tasks"][tid] = {"host": host(), "n": a.n, "correct": c, "pass@1": round(run.pass_at_k(a.n, c, 1), 3),
                             f"pass@{k}": round(run.pass_at_k(a.n, c, min(k, a.n)), 3), "attempts": rows}
    n = len(out["tasks"])
    out["mean_pass@1"] = round(sum(v["pass@1"] for v in out["tasks"].values()) / n, 3)
    out[f"mean_pass@{k}"] = round(sum(v[f"pass@{k}"] for v in out["tasks"].values()) / n, 3)
    save(f"run-{a.agent}", out)
    print(json.dumps({x: out[x] for x in out if x.startswith("mean")}))
    return 0


def cmd_mutation(a) -> int:
    path = RESULTS / "mutation.json"
    out = json.loads(path.read_text()) if path.exists() and a.tasks else {"tasks": {}}
    out["host"] = host()
    for t in tasks(a.tasks):
        d = WORK / "mutation" / t.id
        ref = state(t, "fix", d / "ref")
        rows = []
        for m in mutate.generate(t, ref):
            md = d / m.id
            repo = state(t, "fix", md / "repo")
            (repo / m.path).write_text(m.text)
            why = mutate.stillborn(repo, m)
            if why:
                outcome, res = "stillborn", {"problems": [why], "seconds": 0}
            else:
                res = verify.run(t, repo, md, seed=1)
                outcome = "survived" if res["pass"] else "killed"
            rows.append({"id": m.id, "op": m.op, "where": m.describe(), "outcome": outcome,
                         "problems": res["problems"][:3], "seconds": res["seconds"]})
            print(f"{t.id:36} {m.id} {outcome:9} {m.describe()[:110]}", flush=True)
            shutil.rmtree(md, ignore_errors=True)
        for nm in sorted((t.dir / "near_misses").glob("*.patch")):
            md = d / nm.stem
            repo = workspace.build(t, md / "repo", nm)
            res = verify.run(t, repo, md, seed=1)
            outcome = "survived" if res["pass"] else "killed"
            rows.append({"id": nm.stem, "op": "near-miss", "where": f"tasks/{t.id}/near_misses/{nm.name}",
                         "outcome": outcome, "problems": res["problems"][:3], "seconds": res["seconds"]})
            print(f"{t.id:36} {nm.stem} {outcome:9} near miss", flush=True)
            shutil.rmtree(md, ignore_errors=True)
        gen = [r for r in rows if r["op"] != "near-miss"]
        live = [r for r in gen if r["outcome"] != "stillborn"]
        killed = sum(r["outcome"] == "killed" for r in live)
        near = [r for r in rows if r["op"] == "near-miss"]
        out["tasks"][t.id] = {"host": host(), "mutants": len(gen), "stillborn": len(gen) - len(live), "killed": killed,
                              "survived": len(live) - killed,
                              "score": round(killed / len(live), 3) if live else None,
                              "near_misses": len(near), "near_misses_killed": sum(r["outcome"] == "killed" for r in near),
                              "rows": rows}
        save("mutation", out)
    (RESULTS / "mutation.md").write_text(mutation_table(out))
    return 0


def mutation_table(out: dict) -> str:
    lines = ["| task | mutants | stillborn | killed | survived | caught | near misses killed |",
             "|---|---|---|---|---|---|---|"]
    for tid, v in out["tasks"].items():
        score = "n/a" if v["score"] is None else f"{v['score']:.0%}"
        near = f"{v['near_misses_killed']}/{v['near_misses']}" if v["near_misses"] else "–"
        lines.append(f"| {tid} | {v['mutants']} | {v['stillborn']} | {v['killed']} | {v['survived']} | {score} | {near} |")
    lines += ["", "Survivors:", ""]
    for tid, v in out["tasks"].items():
        for r in v["rows"]:
            if r["outcome"] == "survived":
                lines.append(f"- `{tid}` {r['id']} ({r['op']}): {r['where']}")
    return "\n".join(lines) + "\n"


def cmd_audit(a) -> int:
    for p in map(Path, a.traces):
        report = audit.render(audit.audit(audit.load(p), audit.diff_for(p)))
        if a.write:
            p.with_suffix(".audit.md").write_text(report)
        else:
            print(report)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="verify one task state")
    p.add_argument("task")
    p.add_argument("state", choices=["bug", "fix", "wrong", "cheat"])
    p.add_argument("--seed", type=int, default=1)
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("verify-all", help="bug red, reference green N times, per task")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("tasks", nargs="*")
    p.set_defaults(fn=cmd_verify_all)
    p = sub.add_parser("run", help="agent attempts in the no-network container, pass@k")
    p.add_argument("--agent", choices=sorted(run.AGENTS), required=True)
    p.add_argument("-n", type=int, default=1, help="attempts per task")
    p.add_argument("-k", type=int, default=1)
    p.add_argument("--phase", choices=["all", "agent", "verify"], default="all",
                   help="agent: attempts only; verify: verify the attempts a previous agent phase left")
    p.add_argument("tasks", nargs="*")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("mutation", help="mutants of each reference fix against its verifier")
    p.add_argument("tasks", nargs="*")
    p.set_defaults(fn=cmd_mutation)
    p = sub.add_parser("audit", help="Task Auditor report for traces")
    p.add_argument("traces", nargs="+")
    p.add_argument("--write", action="store_true", help="write TRACE.audit.md next to each trace")
    p.set_defaults(fn=cmd_audit)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
