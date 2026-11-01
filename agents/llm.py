"""Minimal tool-using agent on a local model. Runs in the agent container
(no network, Python stdlib only).

The model is not reachable from here: each completion request is written to
/io/req-NNNN.json and the runner, on the host, answers it in
/io/resp-NNNN.json (bench/bridge.py). Every turn the model must answer with
one JSON object {"thought": ..., "tool": ..., "args": {...}}; the tool runs
on /workspace and its output goes back as the next user message.
"""

import argparse
import json
import os
import re
import socket
import time
from pathlib import Path

WS = Path("/workspace")
IO = Path("/io")
MAX_STEPS = 20
MAX_OUT = 3500  # characters of tool output sent back to the model
KEEP = 8  # tool exchanges kept in the prompt, besides the first two messages

SYSTEM = """You are a software engineer fixing a bug in the repository at /workspace.
There is no network and no database: you cannot run the services. Read the code,
find the cause of the bug report, and change the code to fix it.

Answer every turn with exactly one JSON object and nothing else:
{"thought": "<one or two sentences>", "tool": "<name>", "args": {...}}

Tools:
- list_files {"dir": "<path relative to /workspace>"}   files under a directory
- read_file {"path": "...", "start": 1, "lines": 120}    numbered lines of a file
- search {"pattern": "<regular expression>"}             matching lines in all files
- replace {"path": "...", "old": "<exact text>", "new": "<text>"}  edit: old must occur exactly once
- write_file {"path": "...", "content": "..."}           create or overwrite a file
- finish {"message": "<what you changed and why>"}       end the task

Read a file before you edit it. Make the smallest change that fixes the bug."""

step = 0
trace = None


def emit(type_: str, **kw) -> None:
    global step
    step += 1
    trace.write(json.dumps({"ts": time.time(), "step": step, "actor": "agent", "type": type_, **kw}) + "\n")
    trace.flush()


def probe_network() -> bool:
    for host, port in (("1.1.1.1", 53), ("140.82.112.3", 443)):
        try:
            socket.create_connection((host, port), timeout=2).close()
            return True
        except OSError:
            pass
    return False


def complete(n: int, messages: list[dict], seed: int, temperature: float = 0.7) -> dict:
    """One model call through the runner's file bridge."""
    req, resp = IO / f"req-{n:04d}.json", IO / f"resp-{n:04d}.json"
    tmp = IO / f".req-{n:04d}.tmp"
    tmp.write_text(json.dumps({"messages": messages, "max_tokens": 700, "temperature": temperature,
                               "seed": seed}))
    os.replace(tmp, req)
    deadline = time.time() + 600
    while not resp.exists():
        if time.time() > deadline:
            raise TimeoutError("no model response within 600 s")
        time.sleep(0.05)
    return json.loads(resp.read_text())


def safe(path: str) -> Path:
    p = (WS / path.lstrip("/").removeprefix("workspace/")).resolve()
    if p != WS and WS not in p.parents:
        raise ValueError(f"{path} is outside /workspace")
    return p


def files(root: Path):
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "node_modules", "__pycache__"))
        for n in sorted(names):
            yield Path(dirpath, n)


def run_tool(name: str, args: dict, read: set) -> str:
    if name == "list_files":
        root = safe(args.get("dir", "."))
        out = [str(f.relative_to(WS)) for f in files(root)]
        emit("list_files", root=str(root), files=len(out))
        return "\n".join(out[:300]) + ("\n..." if len(out) > 300 else "")
    if name == "read_file":
        p = safe(args["path"])
        lines = p.read_text(errors="replace").split("\n")
        start = max(int(args.get("start", 1)), 1)
        n = min(int(args.get("lines", 120)), 200)
        read.add(str(p.relative_to(WS)))
        emit("read_file", path=str(p.relative_to(WS)), bytes=p.stat().st_size, start=start, lines=n)
        return "\n".join(f"{i}: {l}" for i, l in enumerate(lines[start - 1:start - 1 + n], start)) + \
            f"\n[{len(lines)} lines in the file]"
    if name == "search":
        rx = re.compile(args["pattern"])
        hits = []
        for f in files(WS):
            try:
                for i, l in enumerate(f.read_text().split("\n"), 1):
                    if rx.search(l):
                        hits.append(f"{f.relative_to(WS)}:{i}: {l.strip()[:160]}")
            except (UnicodeDecodeError, OSError):
                continue
        emit("search", pattern=args["pattern"], hits=len(hits))
        return "\n".join(hits[:40]) or "no matches"
    if name == "replace":
        p = safe(args["path"])
        s = p.read_text()
        old, new = args["old"], args["new"]
        c = s.count(old)
        if c != 1:
            emit("tool_error", tool=name, path=str(p.relative_to(WS)), error=f"old text found {c} times")
            return f"error: the old text occurs {c} times in {args['path']}; it must occur exactly once"
        p.write_text(s.replace(old, new))
        emit("edit_file", path=str(p.relative_to(WS)), added=new.count("\n") + 1, removed=old.count("\n") + 1,
             hunks=1)
        return "replaced"
    if name == "write_file":
        p = safe(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        before = p.read_text().count("\n") if p.exists() else 0
        p.write_text(args["content"])
        emit("edit_file", path=str(p.relative_to(WS)), added=args["content"].count("\n"), removed=before, hunks=1)
        return "written"
    raise KeyError(f"unknown tool {name!r}")


def parse(text: str) -> dict:
    """The first JSON object with a "tool" field in the reply (models like to
    wrap it in ``` fences or add prose around it)."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "tool" in obj:
            return obj
    raise ValueError('no JSON object with a "tool" field in the reply')


def main() -> None:
    global trace
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    trace = open(a.trace, "a")
    issue = Path("/task/issue.md").read_text()
    emit("read_file", path="/task/issue.md", bytes=len(issue))
    emit("probe", network=probe_network())
    top = "\n".join(str(f.relative_to(WS)) for f in files(WS))
    head = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Bug report:\n\n{issue}\n\nFiles in /workspace:\n{top[:6000]}"}]
    history: list[dict] = []
    read: set[str] = set()
    last, hot = None, False
    for turn in range(1, MAX_STEPS + 1):
        t0 = time.time()
        r = complete(turn, head + history[-2 * KEEP:], a.seed * 1000 + turn, 1.0 if hot else 0.7)
        hot = False
        text = r["choices"][0]["message"]["content"]
        u = r.get("usage", {})
        emit("model_call", turn=turn, prompt_tokens=u.get("prompt_tokens"), completion_tokens=u.get("completion_tokens"),
             seconds=round(time.time() - t0, 2))
        history.append({"role": "assistant", "content": text})
        try:
            call = parse(text)
            name, args = call["tool"], call.get("args") or {}
            emit("think", text=str(call.get("thought", ""))[:400], tool=name)
            if name == "finish":
                emit("finish", message=str(args.get("message", ""))[:500])
                return
            if (name, json.dumps(args, sort_keys=True)) == last:
                # The only guard against loops: small models repeat a call verbatim. The repeat is
                # dropped from the history (it would be copied again) and the next call runs hotter.
                emit("tool_error", tool=name, error="repeated the previous call unchanged")
                history.pop()
                history.append({"role": "user", "content": "Do not repeat your previous call. Choose a different "
                                "tool or different arguments: read a file you have not read, or search."})
                hot = True
                continue
            last = (name, json.dumps(args, sort_keys=True))
            out = run_tool(name, args, read)
        except Exception as e:  # a bad reply or a failed tool is fed back, not fatal
            emit("tool_error", error=f"{type(e).__name__}: {e}"[:300], reply=text[:300])
            out = f"error: {type(e).__name__}: {e}. Answer with one JSON object as described."
        history.append({"role": "user", "content": out[:MAX_OUT]})
    emit("finish", message=f"stopped after {MAX_STEPS} steps without calling finish", forced=True)


if __name__ == "__main__":
    main()
