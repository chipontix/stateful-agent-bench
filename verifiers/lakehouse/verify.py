"""Invariant verifier for events-lakehouse.

Usage: python verify.py REPO OUT_JSON

Drives the pipeline in REPO through partition moves and crashes against the
shared Kafka/Postgres (infra/compose.yaml), then compares Postgres and the
Parquet lake with the generator's independent reference. The reference,
generator, codec, producer and comparison code are restored from the pinned
upstream commit before anything runs (bench/workspace.py), so the code under test cannot grade
itself.

Scenario (one fresh topic with 6 partitions and one fresh database):
  A  first third of the stream, workers {0,1,2} and {3,4,5}
  B  second third, partitions moved: {0,2,4} then {1,3,5}; the first worker
     starts with the after-commit failpoint armed (must exit 3) and restarts
  C  last third, one worker for all partitions, SIGKILLed at seeded random
     moments and restarted until it drains the topic
Invariants: Postgres events == reference (count + digest of all columns),
quarantine counts per reason, duplicate count; Parquet lake rows == reference.
Budget: in phase A every worker ingests at least BUDGET_MSG_S messages/s.
"""

import json
import os
import random
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2])
sys.path.insert(0, str(REPO / "src"))

import psycopg  # noqa: E402
from confluent_kafka.admin import AdminClient  # noqa: E402
from lakehouse import produce, reference, verify  # noqa: E402
from lakehouse.generator import Spec  # noqa: E402

N = int(os.environ.get("SAB_LAKEHOUSE_N", 60_000))
SEED = int(os.environ.get("SAB_SEED", 1))
BOOTSTRAP = os.environ.get("SAB_KAFKA", "localhost:29192")
ADMIN_DSN = os.environ.get("SAB_PG", "postgresql://postgres:postgres@localhost:25432/postgres")
BUDGET_MSG_S = float(os.environ.get("SAB_BUDGET_MSG_S", 1500))

name = "lh_" + uuid.uuid4().hex[:10]
work = OUT.parent / (OUT.stem + ".work")
env = dict(os.environ, KAFKA_BOOTSTRAP=BOOTSTRAP, PG_DSN=ADMIN_DSN.rsplit("/", 1)[0] + "/" + name, TOPIC=name,
           LAKE_DIR=str(work / "lake"), PYTHONPATH=str(REPO / "src"))
env.pop("LAKEHOUSE_CRASH_AFTER_COMMIT", None)
problems: list[str] = []
metrics: dict = {}
rng = random.Random(SEED)
spec = Spec(n=N, seed=SEED)


def worker(parts: str | None, extra_env: dict | None = None) -> subprocess.Popen:
    args = [sys.executable, "-m", "lakehouse", "ingest", "--stop-at-eof", "--batch-size=4000", "--max-wait=0.3"]
    if parts:
        args.append("--partitions=" + parts)
    return subprocess.Popen(args, env=env | (extra_env or {}), cwd=work, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def finish(p: subprocess.Popen, what: str, timeout: float = 120) -> dict | None:
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        problems.append(f"{what}: did not drain the topic within {timeout}s")
        return None
    if p.returncode != 0:
        problems.append(f"{what}: exit {p.returncode}: {err.strip()[-400:]}")
        return None
    return json.loads(out.strip().splitlines()[-1])


def phase(label: str, sets: list[str | None]) -> float:
    t0 = time.monotonic()
    procs = [worker(s) for s in sets]
    stats = [finish(p, f"{label} worker {s or 'all'}") for p, s in zip(procs, sets)]
    secs = time.monotonic() - t0
    metrics[label] = {"seconds": round(secs, 2), "workers": stats}
    return secs


def main() -> None:
    work.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    produce.ensure_topic(BOOTSTRAP, name, 6)
    cuts = [0, N // 3, 2 * N // 3, N]
    try:
        # A: steady state, two workers on disjoint partitions.
        produce.run(BOOTSTRAP, name, spec, cuts[0], cuts[1])
        phase("A", ["0,1,2", "3,4,5"])
        # In-process rate (first message -> last commit), not wall time: the
        # interpreter start-up dominates wall time and swings with host load.
        rate = min((w or {}).get("messages_per_s", 0) for w in metrics["A"]["workers"])

        # B: partitions move between workers; one dies right after a commit.
        produce.run(BOOTSTRAP, name, spec, cuts[1], cuts[2])
        crashed = worker("0,2,4", {"LAKEHOUSE_CRASH_AFTER_COMMIT": "1"})
        crashed.communicate(timeout=120)
        if crashed.returncode != 3:
            problems.append(f"B: after-commit failpoint did not fire (exit {crashed.returncode}, want 3)")
        # Restarted alone first: two workers recovering the same batch's
        # Parquet file at once collide on its tmp name (an upstream race, see
        # README "Findings"), which is not what this scenario is about.
        phase("B1", ["0,2,4"])
        phase("B2", ["1,3,5"])

        # C: one worker, SIGKILLed at random moments until it finishes.
        produce.run(BOOTSTRAP, name, spec, cuts[2], cuts[3])
        kills = 0
        while kills < 6:
            p = worker(None)
            try:
                p.wait(timeout=rng.uniform(0.8, 2.5))
            except subprocess.TimeoutExpired:
                p.send_signal(signal.SIGKILL)
                p.wait()
                kills += 1
                continue
            break
        metrics["C_kills"] = kills
        phase("C", [None])

        want = reference.expected(spec)
        pg = verify.from_postgres(env["PG_DSN"])
        lake = verify.from_lake(work / "lake")
        problems.extend(verify.diff("postgres", want, pg, ("events", "digest", "quarantine", "duplicates")))
        problems.extend(verify.diff("parquet", want, lake, ("events", "digest")))
        metrics["expected"] = {"events": want.events, "duplicates": want.duplicates}
        metrics["postgres"] = {"events": pg.events, "duplicates": pg.duplicates}
        metrics["parquet"] = {"events": lake.events}
        metrics["budget"] = {"min_messages_per_s": BUDGET_MSG_S, "ok": rate >= BUDGET_MSG_S}
        if rate < BUDGET_MSG_S:
            problems.append(f"performance budget: slowest phase A worker {rate:.0f} msg/s, budget {BUDGET_MSG_S:.0f}")
    finally:
        admin = AdminClient({"bootstrap.servers": BOOTSTRAP})  # keep a reference until the futures resolve
        for f in admin.delete_topics([name]).values():
            f.result(timeout=30)
        with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        subprocess.run(["rm", "-rf", str(work)])


try:
    main()
except Exception as e:  # a crash of the verifier itself is a failure, with the reason
    import traceback
    problems.append(f"verifier error: {type(e).__name__}: {e} at {traceback.format_exc().splitlines()[-3].strip()}")
OUT.write_text(json.dumps({"pass": not problems, "problems": problems, "metrics": metrics}, indent=2, default=str))
print(json.dumps({"pass": not problems, "problems": problems[:5]}))
sys.exit(0 if not problems else 1)
