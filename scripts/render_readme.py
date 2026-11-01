"""Fills the generated blocks of README.md from results/*.json.

A block is <!-- results:NAME --> ... <!-- /results -->; everything between
the markers is replaced. Run: python3 scripts/render_readme.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
R = ROOT / "results"


def load(name: str) -> dict | None:
    p = R / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def verify_all() -> str:
    d = load("verify-all")
    if not d:
        return "_not run yet_"
    rows = ["| task | with the bug | first problem the verifier reports | reference fix, green runs | verifier time, reference (s) |",
            "|---|---|---|---|---|"]
    for tid, v in d["tasks"].items():
        refs = v["reference"]
        secs = sorted(r["seconds"] for r in refs)
        prob = (v["bug"]["problems"] or ["(none)"])[0].replace("|", "\\|")
        prob = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27}\b", "<id>", prob)[:110]
        rows.append(f"| `{tid}` | {'red' if v['bug_red'] else '**green**'} | {prob} | "
                    f"{v['reference_green']}/{len(refs)} | {secs[0]:.0f}–{secs[-1]:.0f} |")
    rows.append("")
    rows.append(f"`{d['repeat']}` reference runs per task, recorded {d['host']['at']}, host load average "
                f"{d['host']['load_average'].removeprefix('s:').strip()} (1/5/15 min). Reproduce: `make verify-all REPEAT={d['repeat']}`.")
    return "\n".join(rows)


def mutation() -> str:
    d = load("mutation")
    if not d:
        return "_not run yet_"
    rows = ["| task | mutants | stillborn | killed | survived | caught | near misses killed |",
            "|---|---|---|---|---|---|---|"]
    for tid, v in d["tasks"].items():
        gen = [r for r in v["rows"] if r["op"] != "near-miss"]
        near = [r for r in v["rows"] if r["op"] == "near-miss"]
        live = [r for r in gen if r["outcome"] != "stillborn"]
        k = sum(r["outcome"] == "killed" for r in live)
        caught = f"{k / len(live):.0%}" if live else "n/a"
        nk = f"{sum(r['outcome'] == 'killed' for r in near)}/{len(near)}" if near else "–"
        rows.append(f"| `{tid}` | {len(gen)} | {len(gen) - len(live)} | {k} | {len(live) - k} | {caught} | {nk} |")
    rows.append("")
    rows.append(f"Recorded {d['host']['at']}. Reproduce: `make mutation`; per mutant: `results/mutation.json`.")
    return "\n".join(rows)


def survivors() -> str:
    d = load("mutation")
    if not d:
        return "_not run yet_"
    out = []
    for tid, v in d["tasks"].items():
        for r in v["rows"]:
            if r["outcome"] == "survived":
                out.append(f"- `{tid}` {r['id']} ({r['op']}): {r['where']}")
    return "\n".join(out) or "none"


def runs() -> str:
    rows = ["| agent | what it plays back | tasks | attempts per task | mean pass@1 | mean pass@k | audit verdicts |",
            "|---|---|---|---|---|---|---|"]
    what = {"stub-ref": "the reference fix", "stub-wrong": "a plausible wrong fix", "stub-cheat":
            "an edit to the verifier's own files", "stub-coin": "reference or wrong fix, 50/50 per attempt"}
    any_ = False
    for agent in ("stub-ref", "stub-wrong", "stub-cheat", "stub-coin"):
        d = load(f"run-{agent}")
        if not d or "mean_pass@1" not in d:
            continue
        any_ = True
        k = d["k"]
        verdicts: dict = {}
        for v in d["tasks"].values():
            for a in v["attempts"]:
                verdicts[a["verdict"]] = verdicts.get(a["verdict"], 0) + 1
        vs = ", ".join(f"{n}× {x}" for x, n in sorted(verdicts.items()))
        rows.append(f"| `{agent}` | {what[agent]} | {len(d['tasks'])} | {d['attempts_per_task']} | "
                    f"{d['mean_pass@1']:.2f} | pass@{k} {d[f'mean_pass@{k}']:.2f} | {vs} |")
    if not any_:
        return "_not run yet_"
    rows += ["", "Reproduce: `make runs`; traces, diffs and audit reports: `results/traces/<agent>/`."]
    return "\n".join(rows)


BLOCKS = {"verify-all": verify_all, "mutation": mutation, "survivors": survivors, "runs": runs}


def main() -> None:
    p = ROOT / "README.md"
    s = p.read_text()
    for name, fn in BLOCKS.items():
        s, n = re.subn(rf"(<!-- results:{name} -->\n)(?:(?!<!-- results:).)*?(<!-- /results -->)",
                       lambda m: m.group(1) + fn() + "\n" + m.group(2), s, flags=re.S)
        if n == 0:
            print(f"README has no block {name}", file=sys.stderr)
    p.write_text(s)


if __name__ == "__main__":
    main()
