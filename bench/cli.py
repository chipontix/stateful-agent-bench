"""python3 -m bench <command>; see README "Commands"."""

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import audit, mutate, run, verify, workspace

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
    load = subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip().split("load average")[-1]
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "machine": platform.machine(),
            "system": platform.system(), "load_average": load.strip(": ")}


def save(name: str, data: dict) -> None:
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")


def checked(task, kind, d, seed) -> dict:
    repo = state(task, kind, d / "repo")
    res = verify.run(task, repo, d, seed=seed)
    shutil.rmtree(d, ignore_errors=True)
    return res


def short(res: dict) -> dict:
    return {"pass": res["pass"], "seconds": res["seconds"], "tampered": res["tampered"],
            "problems": res["problems"][:5], "metrics": res.get("metrics", {})}


def cmd_check(a) -> int:
    task = workspace.load(a.task)
    res = checked(task, a.state, WORK / "check" / f"{task.id}-{a.state}", a.seed)
    print(json.dumps(short(res), indent=1)[:4000])
    return 0 if res["pass"] else 1


def cmd_verify_all(a) -> int:
    """Per task: bug red, reference green, reference N more times green."""
    out = {"host": host(), "repeat": a.repeat, "tasks": {}}
    ok = True
    for t in tasks(a.tasks):
        d = WORK / "verify-all" / t.id
        bug = checked(t, "bug", d / "bug", 1)
        refs = [checked(t, "fix", d / f"fix-{s}", s) for s in range(1, a.repeat + 1)]
        row = {"bug": short(bug), "reference": [short(r) for r in refs],
               "bug_red": not bug["pass"], "reference_green": sum(r["pass"] for r in refs)}
        row["ok"] = row["bug_red"] and row["reference_green"] == a.repeat
        ok &= row["ok"]
        out["tasks"][t.id] = row
        print(f"{t.id:36} bug {'red' if row['bug_red'] else 'GREEN'}  reference {row['reference_green']}/{a.repeat} green"
              f"  ({bug['seconds']:.0f}s + {sum(r['seconds'] for r in refs):.0f}s)", flush=True)
        save("verify-all", out)
    out["ok"] = ok
    save("verify-all", out)
    return 0 if ok else 1


def cmd_run(a) -> int:
    k = a.k
    out = {"host": host(), "agent": a.agent, "k": k, "attempts_per_task": a.n, "tasks": {}}
    tdir = RESULTS / "traces" / a.agent
    shutil.rmtree(tdir, ignore_errors=True)
    tdir.mkdir(parents=True)
    for t in tasks(a.tasks):
        rows = []
        for i in range(1, a.n + 1):
            r = run.attempt(t, a.agent, i, WORK / "runs" / a.agent / f"{t.id}-{i}", seed=i)
            src = ROOT / r["trace"]
            dst = tdir / f"{t.id}-{i}.jsonl"
            shutil.copy(src, dst)
            shutil.copy(src.parent / "agent.diff", tdir / f"{t.id}-{i}.diff")
            r["trace"] = str(dst.relative_to(ROOT))
            rep = audit.audit(audit.load(dst))
            (tdir / f"{t.id}-{i}.audit.md").write_text(audit.render(rep))
            r["verdict"] = rep["verdict"]
            rows.append(r)
            print(f"{t.id:36} #{i} played {r['played']:5} -> {'pass' if r['pass'] else 'fail'}  {rep['verdict']}", flush=True)
            shutil.rmtree(WORK / "runs" / a.agent / f"{t.id}-{i}", ignore_errors=True)
        c = sum(r["pass"] for r in rows)
        out["tasks"][t.id] = {"n": a.n, "correct": c, "pass@1": round(run.pass_at_k(a.n, c, 1), 3),
                              f"pass@{k}": round(run.pass_at_k(a.n, c, min(k, a.n)), 3), "attempts": rows}
        save(f"run-{a.agent}", out)
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
        live = [r for r in rows if r["outcome"] != "stillborn"]
        killed = sum(r["outcome"] == "killed" for r in live)
        out["tasks"][t.id] = {"mutants": len(rows), "stillborn": len(rows) - len(live), "killed": killed,
                              "survived": len(live) - killed,
                              "score": round(killed / len(live), 3) if live else None, "rows": rows}
        save("mutation", out)
    (RESULTS / "mutation.md").write_text(mutation_table(out))
    return 0


def mutation_table(out: dict) -> str:
    lines = ["| task | mutants | stillborn | killed | survived | caught |", "|---|---|---|---|---|---|"]
    for tid, v in out["tasks"].items():
        score = "n/a" if v["score"] is None else f"{v['score']:.0%}"
        lines.append(f"| {tid} | {v['mutants']} | {v['stillborn']} | {v['killed']} | {v['survived']} | {score} |")
    lines += ["", "Survivors:", ""]
    for tid, v in out["tasks"].items():
        for r in v["rows"]:
            if r["outcome"] == "survived":
                lines.append(f"- `{tid}` {r['id']} ({r['op']}): {r['where']}")
    return "\n".join(lines) + "\n"


def cmd_audit(a) -> int:
    for p in a.traces:
        print(audit.render(audit.audit(audit.load(Path(p)))))
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
    p.add_argument("tasks", nargs="*")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("mutation", help="mutants of each reference fix against its verifier")
    p.add_argument("tasks", nargs="*")
    p.set_defaults(fn=cmd_mutation)
    p = sub.add_parser("audit", help="Task Auditor report for traces")
    p.add_argument("traces", nargs="+")
    p.set_defaults(fn=cmd_audit)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
