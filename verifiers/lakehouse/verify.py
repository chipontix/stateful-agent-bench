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
  D  separate topic (1 partition) and database: two workers own the same
     partition at once, as a worker that keeps running after its partition
     moved would. No producer duplicates here (they would let the two batch
     orders lock the same rows in opposite order); every re-read comes from
     the second owner.
Invariants: Postgres events == reference (count + digest of all columns),
quarantine counts per reason, duplicate count; Parquet lake rows == reference.
     Both owners run without --stop-at-eof. Message 0 is produced alone and
     the rest only once each owner has committed a batch for it, so both are
     live and consuming for the whole stream. They are killed once every
     message was read by both and every batch has its Parquet file.
In D the duplicate count depends on how the owners' batches interleave, so
instead: events and lake == D's reference, and the batches' `inserted` sum
to the rows in events.
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
N_D = int(os.environ.get("SAB_LAKEHOUSE_N_D", 20_000))
SEED = int(os.environ.get("SAB_SEED", 1))
BOOTSTRAP = os.environ.get("SAB_KAFKA", "localhost:23092")
ADMIN_DSN = os.environ.get("SAB_PG_URL", "postgresql://postgres:postgres@localhost:23432/postgres")
BUDGET_MSG_S = float(os.environ.get("SAB_BUDGET_MSG_S", 1500))

name = "lh_" + uuid.uuid4().hex[:10]
name_d = name + "_d"
work = OUT.parent / (OUT.stem + ".work")


def stand_env(nm: str) -> dict:
    e = dict(os.environ, KAFKA_BOOTSTRAP=BOOTSTRAP, PG_DSN=ADMIN_DSN.split("?")[0].rsplit("/", 1)[0] + "/" + nm, TOPIC=nm,
             LAKE_DIR=str(work / nm / "lake"), PYTHONPATH=str(REPO / "src"))
    e.pop("LAKEHOUSE_CRASH_AFTER_COMMIT", None)
    return e


env = stand_env(name)
problems: list[str] = []
metrics: dict = {}
rng = random.Random(SEED)
spec = Spec(n=N, seed=SEED)


def worker(parts: str | None, extra_env: dict | None = None, base: dict | None = None,
           stop_at_eof: bool = True) -> subprocess.Popen:
    args = [sys.executable, "-m", "lakehouse", "ingest", "--batch-size=4000", "--max-wait=0.3"]
    if stop_at_eof:
        args.append("--stop-at-eof")
    if parts:
        args.append("--partitions=" + parts)
    return subprocess.Popen(args, env=(base or env) | (extra_env or {}), cwd=work, stdout=subprocess.PIPE,
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


def two_owners() -> None:
    """D: two workers own partition 0 of a fresh topic at the same time."""
    spec_d = Spec(n=N_D, seed=SEED + 1, dup_rate=0.0)
    env_d = stand_env(name_d)
    produce.ensure_topic(BOOTSTRAP, name_d, 1)
    procs = [worker("0", base=env_d, stop_at_eof=False) for _ in range(2)]
    m = metrics["D"] = {}
    try:
        with psycopg.connect(env_d["PG_DSN"], autocommit=True) as c:
            def batches() -> tuple[int, int, int]:
                try:
                    return c.execute("SELECT count(*), coalesce(sum(messages), 0), count(*) FILTER"
                                     " (WHERE inserted > 0 AND parquet_file IS NULL) FROM ingest_batches").fetchone()
                except psycopg.errors.UndefinedTable:  # no owner has applied the schema yet
                    return 0, 0, 0

            def wait(what: str, done, timeout: float) -> bool:
                t0 = time.monotonic()
                while not done(batches()):
                    if any(p.poll() is not None for p in procs):
                        problems.append(f"D: an owner exited ({[p.poll() for p in procs]}) while waiting for {what}: "
                                        + " ".join(p.stderr.read().strip()[-300:] for p in procs if p.poll() is not None))
                        return False
                    if time.monotonic() - t0 > timeout:
                        problems.append(f"D: timed out after {timeout}s waiting for {what}, batches {batches()}")
                        return False
                    time.sleep(0.1)
                return True

            produce.run(BOOTSTRAP, name_d, spec_d, 0, 1)
            if not wait("both owners to read message 0", lambda b: b[1] >= 2, 60):
                return
            t0 = time.monotonic()
            produce.run(BOOTSTRAP, name_d, spec_d, 1, N_D)
            if not wait("both owners to read every message", lambda b: b[1] >= 2 * N_D and b[2] == 0, 120):
                return
            m["seconds"] = round(time.monotonic() - t0, 2)
            m["batches"], m["messages_read"], _ = batches()
            m["inserted_claimed"], m["duplicates"] = c.execute(
                "SELECT coalesce(sum(inserted), 0), coalesce(sum(duplicates), 0) FROM ingest_batches").fetchone()
    finally:
        for p in procs:
            p.kill()
            p.wait()
    want = reference.expected(spec_d)
    pg = verify.from_postgres(env_d["PG_DSN"])
    problems.extend(verify.diff("D.postgres", want, pg, ("events", "digest", "quarantine")))
    problems.extend(verify.diff("D.parquet", want, verify.from_lake(Path(env_d["LAKE_DIR"])), ("events", "digest")))
    m["events"] = pg.events
    if m["inserted_claimed"] != pg.events:
        problems.append(f"D: batches claim {m['inserted_claimed']} rows inserted, events holds {pg.events}")


def main() -> None:
    work.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
        c.execute(f"CREATE DATABASE {name_d}")
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
        lake = verify.from_lake(Path(env["LAKE_DIR"]))
        problems.extend(verify.diff("postgres", want, pg, ("events", "digest", "quarantine", "duplicates")))
        problems.extend(verify.diff("parquet", want, lake, ("events", "digest")))
        metrics["expected"] = {"events": want.events, "duplicates": want.duplicates}
        metrics["postgres"] = {"events": pg.events, "duplicates": pg.duplicates}
        metrics["parquet"] = {"events": lake.events}
        metrics["budget"] = {"min_messages_per_s": BUDGET_MSG_S, "ok": rate >= BUDGET_MSG_S}
        if rate < BUDGET_MSG_S:
            problems.append(f"performance budget: slowest phase A worker {rate:.0f} msg/s, budget {BUDGET_MSG_S:.0f}")

        two_owners()
    finally:
        admin = AdminClient({"bootstrap.servers": BOOTSTRAP})  # keep a reference until the futures resolve
        for f in admin.delete_topics([name, name_d]).values():
            try:
                f.result(timeout=30)
            except Exception:  # D's topic is not created when an earlier phase raised
                pass
        with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            c.execute(f"DROP DATABASE IF EXISTS {name_d} WITH (FORCE)")
        subprocess.run(["rm", "-rf", str(work)])


try:
    main()
except Exception as e:  # a crash of the verifier itself is a failure, with the reason
    import traceback
    problems.append(f"verifier error: {type(e).__name__}: {e} at {traceback.format_exc().splitlines()[-3].strip()}")
OUT.write_text(json.dumps({"pass": not problems, "problems": problems, "metrics": metrics}, indent=2, default=str))
print(json.dumps({"pass": not problems, "problems": problems[:5]}))
sys.exit(0 if not problems else 1)
