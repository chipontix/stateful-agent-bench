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


def recorded(d: dict) -> str:
    """When each group of task rows was recorded, and the host load at the time."""
    groups: dict = {}
    for tid, v in d["tasks"].items():
        h = v.get("host", d["host"])
        groups.setdefault((h["at"], h["load_average"].removeprefix("s:").strip()), []).append(tid)
    if len(groups) == 1:
        (at, load), = groups
        return f"recorded {at}, host load average {load} (1/5/15 min)"
    return "recorded " + "; ".join(f"{at}, load {load}: {', '.join(f'`{t}`' for t in ts) if len(ts) < 3 else f'{len(ts)} tasks'}"
                                   for (at, load), ts in sorted(groups.items()))


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
    for tid, v in d["tasks"].items():  # the budgets that are rates: how far the reference is from them
        ms = [r["metrics"] for r in v["reference"] if "tokens_per_s" in r.get("metrics", {})]
        if ms:
            rate = sorted(m["tokens_per_s"] for m in ms)
            hits = sorted(m["prefix_cache_hit_blocks"] for m in ms)
            pre = sorted(m["preemptions"] for m in ms)
            rows.append(f"`{tid}`, reference: {rate[0]:.0f}–{rate[-1]:.0f} generated tokens/s (budget "
                        f"{ms[0]['budget_tokens_per_s']:.0f}, margin ×{rate[0] / ms[0]['budget_tokens_per_s']:.1f}), "
                        f"{hits[0]}–{hits[-1]} blocks served by the prefix cache (minimum "
                        f"{ms[0]['min_prefix_cache_hit_blocks']}), {pre[0]}–{pre[-1]} preemptions, "
                        f"{max(m['outputs_differing'] for m in ms)} outputs differing from the solo run.")
            rows.append("")
    rows.append(f"`{d['repeat']}` reference runs per task, {recorded(d)}. Reproduce: `make verify-all REPEAT={d['repeat']}`.")
    return "\n".join(rows)


def mutation() -> str:
    d = load("mutation")
    if not d:
        return "_not run yet_"
    rows = ["| task | mutants | stillborn | killed | survived | of which equivalent | caught | caught, equivalents excluded | near misses killed |",
            "|---|---|---|---|---|---|---|---|---|"]
    tot = [0, 0, 0]
    for tid, v in d["tasks"].items():
        gen = [r for r in v["rows"] if r["op"] != "near-miss"]
        near = [r for r in v["rows"] if r["op"] == "near-miss"]
        live = [r for r in gen if r["outcome"] != "stillborn"]
        k = sum(r["outcome"] == "killed" for r in live)
        eq = equivalent(tid)
        n_eq = sum(r["outcome"] == "survived" and r["id"] in eq for r in live)
        caught = f"{k / len(live):.0%}" if live else "n/a"
        caught_x = f"{k / (len(live) - n_eq):.0%}" if len(live) > n_eq else "n/a"
        nk = f"{sum(r['outcome'] == 'killed' for r in near)}/{len(near)}" if near else "–"
        tot = [tot[0] + len(live), tot[1] + k, tot[2] + n_eq]
        rows.append(f"| `{tid}` | {len(gen)} | {len(gen) - len(live)} | {k} | {len(live) - k} | {n_eq} | {caught} | {caught_x} | {nk} |")
    rows.append(f"| all | | | {tot[1]} of {tot[0]} live | | {tot[2]} | {tot[1] / tot[0]:.0%} | "
                f"{tot[1] / (tot[0] - tot[2]):.0%} | |")
    rows.append("")
    rows.append("Equivalent = argued by hand in `tasks/<id>/equivalent.json`; each argument is listed below.")
    rows.append(f"R{recorded(d)[1:]}. Reproduce: `make mutation`; per mutant: `results/mutation.json`.")
    return "\n".join(rows)


def equivalent(tid: str) -> dict:
    p = ROOT / "tasks" / tid / "equivalent.json"
    return json.loads(p.read_text()) if p.exists() else {}


def survivors() -> str:
    d = load("mutation")
    if not d:
        return "_not run yet_"
    out = []
    for tid, v in d["tasks"].items():
        eq = equivalent(tid)
        for r in v["rows"]:
            if r["outcome"] == "survived":
                why = f" **Equivalent:** {eq[r['id']]}." if r["id"] in eq else ""
                out.append(f"- `{tid}` {r['id']} ({r['op']}): {r['where']}.{why}")
    return "\n".join(out) or "none"


def runs() -> str:
    rows = ["| agent | what it plays back | tasks | attempts per task | mean pass@1 | mean pass@k | audit verdicts |",
            "|---|---|---|---|---|---|---|"]
    what = {"stub-ref": "the reference fix", "stub-wrong": "a plausible wrong fix", "stub-cheat":
            "an edit that gets past the verifier instead of fixing the bug", "stub-coin": "reference or wrong fix, 50/50 per attempt",
            "llm-qwen-coder-1.5b": "its own edits: Qwen2.5-Coder-1.5B-Instruct Q4_K_M on llama.cpp, 20 tool calls max",
            "llm-qwen-coder-1.5b-hint": "the same agent, told which files the reference fix changes"}
    any_ = False
    for agent in ("stub-ref", "stub-wrong", "stub-cheat", "stub-coin", "llm-qwen-coder-1.5b", "llm-qwen-coder-1.5b-hint"):
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


def when(d: dict) -> str:
    a, b = d.get("recorded") or [d["host"]["at"]] * 2
    days = sorted({r[:10] for r in (a, b)})
    return a if len(days) == 1 else f"between {a} and {b} (the attempts of a task added later were run later)"


def llm() -> str:
    d, h = load("llm-analysis"), load("llm-analysis-hint")
    if not d:
        return "_not run yet_"
    s, n = d["summary"], d["summary"]["attempts"]
    hs = h["summary"] if h else None
    rows = ["| what the attempts did | no hint | told the file |" if hs else "| what the attempts did | attempts |",
            "|---|---|---|" if hs else "|---|---|"]
    for key, text in (("passed", "passed the verifier"), ("no_change", "changed nothing"),
                      ("read_the_file_with_the_bug", "read the file the reference fix changes"),
                      ("changed_the_file_with_the_bug", "changed that file"),
                      ("changed_only_other_files", "changed only other files (README, new placeholder files, comments)"),
                      ("changed_a_protected_file_or_failpoint", "changed a protected file or removed the failpoint"),
                      ("called_finish", "called finish"), ("ran_out_of_steps", "ran out of its 20 steps"),
                      ("attempts_with_a_repeated_call", "repeated a call verbatim at least once")):
        rows.append(f"| {text} | {s[key]} of {n} |" + (f" {hs[key]} of {hs['attempts']} |" if hs else ""))
    rows += ["", f"No hint: {s['model_calls']} model calls, {s['completion_tokens']} completion tokens in all, recorded "
             f"{when(d)}. Reproduce: `make runs-llm`; per attempt: "
             "`results/llm-analysis.json`, traces and audits: `results/traces/llm-qwen-coder-1.5b/`."]
    if hs:
        rows += ["", f"Told the file: {hs['model_calls']} model calls, {hs['completion_tokens']} completion tokens in all,"
                 f" recorded {when(h)}. Reproduce: `make runs-llm-hint`; per attempt: "
                 "`results/llm-analysis-hint.json`, traces and audits: `results/traces/llm-qwen-coder-1.5b-hint/`."]
    return "\n".join(rows)


BLOCKS = {"llm": llm, "verify-all": verify_all, "mutation": mutation, "survivors": survivors, "runs": runs}


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
