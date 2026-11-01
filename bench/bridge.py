"""The local model, served on the host, and the file bridge that lets an agent
in a --network none container talk to it.

The agent writes io/req-NNNN.json (an OpenAI-style chat request); answer()
forwards it to llama-server and writes io/resp-NNNN.json. The container never
gets a socket to anything.
"""

import glob
import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("SAB_MODEL_PORT", 23200))
MODEL = os.environ.get("SAB_MODEL", os.path.expanduser(
    "~/.cache/huggingface/hub/models--Qwen--Qwen2.5-Coder-1.5B-Instruct-GGUF/snapshots/*/"
    "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"))
CTX = 16384


class Server:
    """llama-server for the duration of a `with` block (stopped by its own PID)."""

    def __init__(self, log: Path):
        self.log = log

    def __enter__(self):
        paths = glob.glob(MODEL)
        if not paths:
            raise FileNotFoundError(f"no model at {MODEL}; see README 'Local model agent'")
        self.model = paths[0]
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(["llama-server", "-m", self.model, "--port", str(PORT), "-c", str(CTX),
                                      "-ngl", "99", "--parallel", "1"],
                                     stdout=self.log.open("w"), stderr=subprocess.STDOUT)
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
                return self
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"llama-server exited, see {self.log}")
                time.sleep(1)
        raise TimeoutError("llama-server not healthy after 120 s")

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def answer(req: Path) -> None:
    body = json.loads(req.read_text())
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions", data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=600) as f:
            resp = json.loads(f.read())
    except OSError as e:  # the agent sees a failed call as an empty reply and carries on
        resp = {"choices": [{"message": {"content": f"(model call failed: {e})"}}], "usage": {}}
    out = req.with_name(req.name.replace("req-", "resp-"))
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(resp))
    os.replace(tmp, out)


class Bridge:
    """Answers the requests that appear in io_dir until stopped."""

    def __init__(self, io_dir: Path):
        self.io, self.stop = io_dir, threading.Event()
        self.calls = 0
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self) -> None:
        done = set()
        while not self.stop.is_set():
            for req in sorted(self.io.glob("req-*.json")):
                if req.name not in done:
                    answer(req)
                    done.add(req.name)
                    self.calls += 1
            time.sleep(0.05)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(5)
