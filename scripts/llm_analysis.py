"""What the local-model agent did, counted from its traces: did it find the
file with the bug, did it change it, how it failed. Writes
results/llm-analysis.json (results/llm-analysis-hint.json for the hinted
agent). Run: python3 scripts/llm_analysis.py [AGENT]"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bench import audit, workspace  # noqa: E402
from bench.mutate import hunks  # noqa: E402

agent = sys.argv[1] if len(sys.argv) > 1 else "llm-qwen-coder-1.5b"
run = json.loads((ROOT / "results" / f"run-{agent}.json").read_text())
rows = []
for tid, v in run["tasks"].items():
    ref_files = {p for p, *_ in hunks(workspace.load(tid).patch("fix").read_text())}
    for a in v["attempts"]:
        ev = audit.load(ROOT / a["trace"])
        ag = [e for e in ev if e["actor"] == "agent"]
        diff = next(e for e in ev if e["type"] == "diff")
        result = next(e for e in ev if e["type"] == "result")
        read = {e["path"] for e in ag if e["type"] == "read_file"}
        rows.append({
            "task": tid, "attempt": a["attempt"], "pass": a["pass"], "verdict": a["verdict"],
            "model_calls": sum(e["type"] == "model_call" for e in ag),
            "completion_tokens": sum(e.get("completion_tokens") or 0 for e in ag if e["type"] == "model_call"),
            "tool_errors": sum(e["type"] == "tool_error" for e in ag),
            "repeated_calls": sum(e["type"] == "tool_error" and "repeated" in e.get("error", "") for e in ag),
            "read_reference_file": bool(read & ref_files),
            "changed_files": diff["files"],
            "changed_reference_file": bool(set(diff["files"]) & ref_files),
            "protected_changed": diff["protected"],
            "called_finish": any(e["type"] == "finish" and not e.get("forced") for e in ag),
            "stage": result["stage"],
            "first_problem": (result["problems"] or [""])[0][:160],
        })
n = len(rows)
summary = {
    "attempts": n,
    "passed": sum(r["pass"] for r in rows),
    "no_change": sum(not r["changed_files"] for r in rows),
    "read_the_file_with_the_bug": sum(r["read_reference_file"] for r in rows),
    "changed_the_file_with_the_bug": sum(r["changed_reference_file"] for r in rows),
    "changed_only_other_files": sum(bool(r["changed_files"]) and not r["changed_reference_file"] for r in rows),
    "changed_a_protected_file_or_failpoint": sum(r["verdict"].startswith(("INVALID", "NOT RESOLVED, and")) for r in rows),
    "called_finish": sum(r["called_finish"] for r in rows),
    "ran_out_of_steps": sum(not r["called_finish"] for r in rows),
    "attempts_with_a_repeated_call": sum(r["repeated_calls"] > 0 for r in rows),
    "model_calls": sum(r["model_calls"] for r in rows),
    "completion_tokens": sum(r["completion_tokens"] for r in rows),
}
# a run of named tasks merges into the file, so the time comes from each attempt's verifier event
recorded = sorted(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(next(e for e in audit.load(ROOT / a["trace"])
                                                                    if e["type"] == "result")["ts"]))
                  for v in run["tasks"].values() for a in v["attempts"])
out = {"agent": agent, "host": run["host"], "recorded": [recorded[0], recorded[-1]], "summary": summary, "attempts": rows}
suffix = agent.removeprefix("llm-qwen-coder-1.5b")
(ROOT / "results" / f"llm-analysis{suffix}.json").write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps(summary, indent=1))
