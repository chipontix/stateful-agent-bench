"""Stub agent: runs in the agent container (no network) and plays back a
prepared patch, logging what it does as a trace an auditor can read.

/task/issue.md       the bug report, as a real agent would get it
/task/solution.patch the patch to play back (the runner picks it)
/workspace           the repository to change
Trace: one JSON object per line on --trace. Python stdlib only.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

WS = Path("/workspace")
step = 0
trace = None


def emit(type_: str, **kw) -> None:
    global step
    step += 1
    trace.write(json.dumps({"ts": time.time(), "step": step, "actor": "agent", "type": type_, **kw}) + "\n")
    trace.flush()


def probe_network() -> bool:
    """True if anything outside the container is reachable."""
    for host, port in (("1.1.1.1", 53), ("140.82.112.3", 443)):  # an IP each, so DNS is not needed
        try:
            socket.create_connection((host, port), timeout=2).close()
            return True
        except OSError:
            pass
    return False


def parse(patch: str) -> list[tuple[str, list]]:
    """[(path, [(old_start, old_lines, new_lines), ...])] from a git diff.
    Hunk bodies are read by their line counts, so a removed line that starts
    with "--" is not mistaken for a file header."""
    files, hunks = [], None
    left = (0, 0)  # old, new lines still to read in the current hunk
    for line in patch.splitlines():
        if left != (0, 0):
            line = line or " "  # an empty context line can lose its leading space
            _, old, new = hunks[-1]
            if line[0] in " -":
                old.append(line[1:])
            if line[0] in " +":
                new.append(line[1:])
            left = (left[0] - (line[0] in " -"), left[1] - (line[0] in " +"))
        elif line.startswith("+++ "):
            hunks = []
            files.append((line[4:].removeprefix("b/"), hunks))
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
            hunks.append((int(m.group(1)), [], []))
            left = (int(m.group(2) or 1), int(m.group(3) or 1))
    return files


def apply(path: str, hunks: list) -> tuple[int, int]:
    f = WS / path
    lines = f.read_text().split("\n") if f.exists() else []
    offset, added, removed = 0, 0, 0
    for start, old, new in hunks:
        at = max(start - 1 + offset, 0)
        if lines[at:at + len(old)] != old:  # context moved: search for it
            at = next(i for i in range(len(lines)) if lines[i:i + len(old)] == old)
        lines[at:at + len(old)] = new
        offset += len(new) - len(old)
        added += sum(1 for x in new if x not in old)
        removed += sum(1 for x in old if x not in new)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(lines) if lines and lines[-1] == "" else "\n".join(lines) + "\n")
    return added, removed


def main() -> None:
    global trace
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    a = ap.parse_args()
    trace = open(a.trace, "a")
    issue = Path("/task/issue.md").read_text()
    emit("read_file", path="/task/issue.md", bytes=len(issue))
    emit("probe", network=probe_network())
    n = sum(len(fs) for _, _, fs in os.walk(WS) if ".git" not in _)
    emit("list_files", root="/workspace", files=n)
    patch = Path("/task/solution.patch").read_text()
    files = parse(patch)
    for path, _ in files:
        if (WS / path).exists():
            emit("read_file", path=path, bytes=(WS / path).stat().st_size)
    for path, hunks in files:
        added, removed = apply(path, hunks)
        emit("edit_file", path=path, added=added, removed=removed, hunks=len(hunks))
    for cmd in (["go", "version"], ["python3", "-m", "compileall", "-q", "/workspace/src"]):
        if cmd[0] == "python3" and not (WS / "src").exists():
            continue
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            emit("run_command", cmd=" ".join(cmd), exit=p.returncode, output=(p.stdout + p.stderr)[-500:])
        except FileNotFoundError:
            emit("run_command", cmd=" ".join(cmd), exit=127, output=f"{cmd[0]}: not found")
    emit("finish", message=f"changed {len(files)} file(s): " + ", ".join(p for p, _ in files))


if __name__ == "__main__":
    main()
