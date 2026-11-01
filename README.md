# stateful-agent-bench

[![ci](https://github.com/chipontix/stateful-agent-bench/actions/workflows/ci.yml/badge.svg)](https://github.com/chipontix/stateful-agent-bench/actions/workflows/ci.yml)

Tasks for coding agents taken from three stateful systems: a saga ledger, a
CDC search pipeline and a Kafka → Postgres + Parquet lakehouse. Each task
starts from a pinned commit of the real project with a realistic bug patched
in. The fix is graded by running the real services under SIGKILLs, restarts,
partition moves and write load, and then checking the system's invariants
against an independent reference. Unit tests play no part in the grade.

```bash
make up KAFKA=native deps  # Postgres in Docker, Kafka on the host JVM (the stand's requirement), upstream clones, Python env
make verify-all       # per task: bug red, reference fix green, 3 reference runs without a flake
make mutation         # mutants of every reference fix against its verifier
make runs             # stub agents in a container with --network none, traces, audits, pass@k
make test             # the tooling's own checks (no brokers, no Docker)
```

## How it was built

The code, the tasks, the verifiers and this documentation were written by the
AI coding agent Claude Code (Anthropic), from a specification by Rabiya and
under her direction. She chose the projects, the kinds of bugs and the roles
the bench is meant to show. Each claim below was checked by running it, and
the commands that reproduce the numbers sit next to them. The three upstream
projects are hers ([ledger-saga](https://github.com/chipontix/ledger-saga),
[pg-search-sync](https://github.com/chipontix/pg-search-sync),
[events-lakehouse](https://github.com/chipontix/events-lakehouse)).

## Tasks

| task | project | what the bug report says | what the bug is (hidden from the agent) |
|---|---|---|---|
| `ledger-retry-double-charge` | ledger-saga (Go, Postgres, Kafka) | customers charged twice when the ledger is slow | the ledger no longer replays a step it already decided (only the transport inbox dedups, and every retry has a new message id); the `(saga, step)` unique key is gone |
| `ledger-hold-release-race` | ledger-saga | money stays held after cancelled transfers | the per-saga advisory lock was removed as "redundant with the row lock": a hold and its release read the saga's prior steps concurrently, and the release records a tombstone for a hold that commits a moment later |
| `pgsync-slot-dropped-on-restart` | pg-search-sync (Go, Postgres, Kafka) | changes made while cdc restarts never reach search | on start the connector drops an inactive replication slot "so it cannot pin WAL"; after a kill the slot is inactive, so the WAL written while the connector was down is never read |
| `pgsync-deletes-replica-identity` | pg-search-sync | cdc crash-loops after the first delete | the publication asks for `REPLICA IDENTITY DEFAULT` "to halve WAL", the connector applies it, and a delete then carries no `version` for its tombstone |
| `lakehouse-dups-after-rebalance` | events-lakehouse (Python, Kafka, Postgres, Parquet) | duplicate events in the lake after partitions moved between workers | dedup moved into a per-process set of seen ids and `RETURNING` dropped: a new owner of a partition re-inserts what the previous owner loaded, Postgres silently skips it and the lake gets it twice |
| `lakehouse-offsets-after-parquet` | events-lakehouse | duplicate counts too high after a worker crash | the next offset is stored in its own transaction after the Parquet write "so the position never runs ahead of the lake"; a crash between the two replays a committed batch |

The courier-shifts-userver task from the original plan is not here. A userver
build needs much more than the 20 GB of free disk the machine had while six
projects were being worked on in parallel, so a second task from each of the
other three projects took its place.

Each task directory holds `issue.md` (what the agent sees), `bug.patch` (on
top of the pinned commit in `task.json`), `fix.patch` (the reference: the
original code), `wrong.patch` (a plausible fix that is wrong), `cheat.patch`
(an edit that gets past the verifier instead of fixing the bug: a change to
the checker's files, a disarmed crash point, a swallowed error) and, for some
tasks, `near_misses/`.
The agent's repository is a fresh git repo with one commit, so neither history
nor `git diff` gives the bug away. `scripts/author_patches.py` writes all the
patches from readable before/after edits.

## Why invariants and not unit tests

All six bugs pass the code paths a unit test would normally exercise. None of
them shows up in one process, on one partition or in the first delivery. Each
one needs a second actor, a retry or a crash in a particular gap:

- **Double charge** needs a retry with a new message id while the first
  attempt's decision is already committed. A test of `Decide` passes,
  because `Decide` is still correct; the caller stopped consulting the
  saga's history. `sabrace` sends every retry at the same instant as the
  first attempt, and the crash harness produces such retries on every run
  (timeouts, deferred deliveries, redeliveries, SIGKILL after commit). Both
  check the money: "moved src −2x".
- **Hold/release race** needs two transactions for one saga in flight at
  once, under READ COMMITTED, reading the saga's prior steps before either
  commits. That interleaving only happens when the ledger catches up on a
  backlog after a crash. A test that sends hold and then release in order
  passes. The crash harness alone reached the interleaving in only 4 of 40
  runs (8 runs per verification, 5 verifications), so a plausible wrong fix
  got through once in two tries
  (`results/raw/race-detection-crash-harness-only.txt`). That is why the ledger verifier starts with
  `sabrace`, which sends hold and release of one saga at the same instant
  300 times per verification.
- **Slot dropped on restart** needs the connector to die while writes
  continue. The code that drops the slot even has a careful `AND NOT
  active`. The verifier kills it twice and restarts it once under 400
  writes/s, then resolves the topic per id the way Elasticsearch's external
  versioning would.
- **Replica identity** only fails once the connector has altered the table
  and a delete arrives. The fixture rows in a unit test still carry a
  `version`.
- **Dups after rebalance** need a duplicate whose original was loaded by
  *another* process. Inside one worker the in-memory set works perfectly, and
  the Postgres table stays correct (the primary key still rejects the row).
  Only the lake and the duplicate counter are wrong, and only a comparison
  with the generator's ground truth shows that.
- **Offsets after Parquet** looks *safer* than the original when read: it
  says "the position only moves once the batch is in the lake". It breaks
  only on a crash between two commits, which the verifier arms with the
  project's own failpoint.

An invariant is stated about the outcome, so it does not care which code path
an agent changes. Every correct fix passes, including ones other than the
reference. Every incorrect one fails, as long as the scenario reaches the
broken interleaving. That last condition is the real limit of this bench, and
the mutation results below measure it.

## Verifiers

All three run the project's real binaries on the host against one Postgres and
one Kafka (`infra/`). Before a run, the files that grade the result are put
back to the pinned upstream version (`task.json` → `protected`), so an agent
cannot weaken its own grader. The runner reports any such edit (see the
auditor).

| project | scenario | invariants | performance budget |
|---|---|---|---|
| ledger-saga | (1) `verifiers/ledger/sabrace.go`: the repository's real `ledger.Store` on a fresh database, 600 sagas, and for each one, after a start barrier, either hold ‖ release or hold ‖ retried hold, then capture ‖ retried capture and credit ‖ retried credit, with handler errors retried as the consumer would; (2) the project's crash harness (`ledgersaga chaos`): three services as real processes, a seeded workload of 200 transfers over the HTTP API, duplication, reordering and late redelivery on every run; crash points `none`, `ledger.before_commit`, `ledger.after_commit`, `orch.reply.after_commit` in parallel, each SIGKILLing the process once and restarting it | (1) per saga, the recorded operations are ones its final state allows (`saga.CheckEffects`, the project's rule), each step recorded once, exactly ±amount moved; journal balanced, nothing held. (2) the project's own audit (`internal/check`): journal sums to zero per operation and globally, balances equal the journal, no negative balance, nothing held at rest, customer money equals the opening total, every saga terminal with exactly the ledger effects its state allows and exactly ±amount moved, legal transition logs, one transfer per idempotency key | (1) ≥ 150 handled commands/s; (2) every run quiesces within 90 s of the workload's end |
| pg-search-sync | `verifiers/pgsync/sabcheck.go`: fresh database and topic, real `cdc` binary, 16 s of 400 writes/s (60% update, 20% insert, 12% delete, 8% re-insert of a deleted id), `kill -9` twice and one SIGTERM restart while the writes continue | the topic resolved per id (highest version wins, as with ES `version_type=external`) equals the table: every row searchable at its current version, every deleted row a tombstone, nothing extra; the connector never exits on its own | converged within 30 s after the writes stop |
| events-lakehouse | `verifiers/lakehouse/verify.py`: 60k generated messages (5% duplicates, 3% late, 0.5% bad) in three thirds: two workers on {0,1,2}/{3,4,5}; partitions moved to {0,2,4}/{1,3,5}, the first started with the after-commit failpoint armed; one worker for all partitions SIGKILLed at seeded random moments | Postgres `events` and the Parquet lake equal the generator's reference row for row (count + order-independent digest of all columns), quarantine counts per reason, duplicate count | every phase-1 worker ingests ≥ 1,500 msg/s |

The budgets are loose on purpose. They exist to fail a "fix" that serialises
the system or commits per row, not to measure performance, and they have to
hold on a laptop shared with other workloads.

### Results

<!-- results:verify-all -->
| task | with the bug | first problem the verifier reports | reference fix, green runs | verifier time, reference (s) |
|---|---|---|---|---|
| `lakehouse-dups-after-rebalance` | red | postgres.duplicates: want 3006, got 1947 | 3/3 | 20–27 |
| `lakehouse-offsets-after-parquet` | red | postgres.duplicates: want 3006, got 6985 | 3/3 | 20–40 |
| `ledger-hold-release-race` | red | concurrent: 289 sagas with disallowed effects, 0 applied twice, 0 with wrong money (of 600) | 3/3 | 43–50 |
| `ledger-retry-double-charge` | red | concurrent: 0 sagas with disallowed effects, 900 applied twice, 300 with wrong money (of 600) | 3/3 | 47–51 |
| `pgsync-deletes-replica-identity` | red | 1045 missing, 138 stale, 47 extra documents | 3/3 | 27–28 |
| `pgsync-slot-dropped-on-restart` | red | 29 missing, 63 stale, 53 extra documents | 3/3 | 28–31 |

`3` reference runs per task, recorded 2026-11-01T08:20:28Z, host load average 42.48 48.19 54.02 (1/5/15 min). Reproduce: `make verify-all REPEAT=3`.
<!-- /results -->

## Mutation testing of the verifiers

`bench/mutate.py` mutates only the lines the reference fix touches:
`revert-hunk` undoes one hunk of the fix (a partial fix), `delete-line` drops
one added line, and `flip` inverts one operator or literal. Comments and
docstrings are never mutated. A mutant that does not build is "stillborn" and
stays out of the score, because the compiler caught it and the verifier did
not. The hand-written `near_misses/` run next to the generated mutants.

<!-- results:mutation -->
| task | mutants | stillborn | killed | survived | of which equivalent | caught | caught, equivalents excluded | near misses killed |
|---|---|---|---|---|---|---|---|---|
| `lakehouse-dups-after-rebalance` | 7 | 0 | 6 | 1 | 1 | 86% | 100% | 0/1 |
| `lakehouse-offsets-after-parquet` | 6 | 2 | 3 | 1 | 1 | 75% | 100% | – |
| `ledger-hold-release-race` | 4 | 1 | 2 | 1 | 0 | 67% | 67% | – |
| `ledger-retry-double-charge` | 12 | 3 | 7 | 2 | 2 | 78% | 100% | – |
| `pgsync-deletes-replica-identity` | 4 | 0 | 2 | 2 | 2 | 50% | 100% | 1/1 |
| `pgsync-slot-dropped-on-restart` | 4 | 4 | 0 | 0 | 0 | n/a | n/a | 0/1 |
| all | | | 20 of 27 live | | 6 | 74% | 95% | |

Equivalent = argued by hand in `tasks/<id>/equivalent.json`; each argument is listed below.
Recorded 2026-11-01T09:08:16Z. Reproduce: `make mutation`; per mutant: `results/mutation.json`.
<!-- /results -->

Surviving mutants:

<!-- results:survivors -->
- `lakehouse-dups-after-rebalance` m02 (revert-hunk): src/lakehouse/ingest.py:105 hunk reverted to the buggy version. **Equivalent:** re-adds `self.seen = set()` to __init__; nothing reads it in the reference.
- `lakehouse-dups-after-rebalance` reload-seen-at-start (near-miss): tasks/lakehouse-dups-after-rebalance/near_misses/reload-seen-at-start.patch.
- `lakehouse-offsets-after-parquet` m02 (revert-hunk): src/lakehouse/ingest.py:167 hunk reverted to the buggy version. **Equivalent:** re-adds a second upsert of the same offsets after the Parquet write; the batch transaction already stored them, and the upsert is idempotent.
- `ledger-hold-release-race` m04 (delete-line): internal/ledger/store.go:40 deleted `return "", err`.
- `ledger-retry-double-charge` m01 (revert-hunk): internal/db/schema.sql:68 hunk reverted to the buggy version. **Equivalent:** drops UNIQUE (saga_id, step) but keeps the replay check; under the per-saga advisory lock the replay alone already prevents a second decision, so only the safety net is gone.
- `ledger-retry-double-charge` m04 (delete-line): internal/db/schema.sql:71 deleted `at         timestamptz NOT NULL DEFAULT now(),`. **Equivalent:** drops the `at` column of ledger.operations; nothing reads or writes it explicitly.
- `pgsync-deletes-replica-identity` m01 (revert-hunk): deploy/postgres/init.sql:28 hunk reverted to the buggy version. **Equivalent:** edits deploy/postgres/init.sql, which the verifier never applies (it uses the pinned upstream schema) and which the connector overrides anyway.
- `pgsync-deletes-replica-identity` m03 (delete-line): deploy/postgres/init.sql:33 deleted `ALTER TABLE products REPLICA IDENTITY FULL;`. **Equivalent:** same file as m01.
- `pgsync-slot-dropped-on-restart` drop-on-shutdown (near-miss): tasks/pgsync-slot-dropped-on-restart/near_misses/drop-on-shutdown.patch.
<!-- /results -->

The survivors that are not equivalent are the bench's measured blind spots:

- **`ledger-hold-release-race` m04** deletes the `return` after a failed
  advisory-lock statement. Neither scenario makes that statement fail, so an
  error path that silently carries on is invisible. Fault injection into
  Postgres calls would close this gap; it is not built.
- **`reload-seen-at-start`** fills the in-memory dedup set from Postgres at
  startup. That is correct when workers take turns on a partition and wrong
  when two of them own it at once. The lakehouse scenario never has two
  owners at once (see Limitations).
- **`drop-on-shutdown`** moves the slot drop from startup to shutdown, after
  `connector.Close()`. It never takes effect in this scenario: a drop would
  lose the writes made in the restart gap, and none were lost. Either the
  process exits before the drop runs, or the slot is still active at that
  moment. In production this would depend on timing.
- **`pgsync-slot-dropped-on-restart` has no live generated mutants.** Its
  fix only deletes code, so undoing one hunk either leaves an unused import
  or function (which Go rejects) or reintroduces the whole bug. The wrong fix
  and the near miss are what test this verifier.

## Runner and agents

`python3 -m bench run --agent A -n N -k K` runs each attempt like this:

1. It builds the task repository (pinned commit + `bug.patch`, one fresh
   commit).
2. It starts the agent with `docker run --network none --memory 512m --cpus
   1`. The repository is mounted at `/workspace`; `issue.md` and nothing else
   from the task is mounted at `/task`.
3. It takes the agent's diff, runs the verifier on a copy with the protected
   files restored, and writes everything to one JSONL trace: runner events,
   agent events (each read, edit, command and network probe) and the
   verifier result.
4. It computes pass@1 and pass@k with the unbiased estimator (Chen et al.
   2021) over the N attempts.

The agents here are stubs that play back a prepared patch through a small
stdlib-only patch applier inside the container. They show that the runner
and the verifiers separate a correct fix from a wrong one, and from an
attempt to edit the grader. They say nothing about any model. The first thing
a stub does is probe the network (two IP addresses, no DNS needed), and the
trace records the result.

<!-- results:runs -->
| agent | what it plays back | tasks | attempts per task | mean pass@1 | mean pass@k | audit verdicts |
|---|---|---|---|---|---|---|
| `stub-ref` | the reference fix | 6 | 1 | 1.00 | pass@1 1.00 | 6× RESOLVED |
| `stub-wrong` | a plausible wrong fix | 6 | 1 | 0.00 | pass@1 0.00 | 6× NOT RESOLVED |
| `stub-cheat` | an edit that gets past the verifier instead of fixing the bug | 6 | 1 | 0.00 | pass@1 0.00 | 1× NOT RESOLVED, 5× NOT RESOLVED, with an attempt to game the verifier |
| `stub-coin` | reference or wrong fix, 50/50 per attempt | 6 | 4 | 0.58 | pass@2 0.72 | 10× NOT RESOLVED, 14× RESOLVED |

Reproduce: `make runs`; traces, diffs and audit reports: `results/traces/<agent>/`.
<!-- /results -->

### Trace auditor

`python3 -m bench audit TRACE.jsonl` turns a trace into a Task Auditor style
report with three sections: what the agent did (every step, numbered as in
the trace), where it went wrong, and whether it got around the verifier. It
uses rules, not a model, so the same trace always gets the same report. The
rules:

- an edit to a file the agent never read;
- no passing test run before finishing;
- files changed that the reference does not touch, or reference files left
  untouched in a failed attempt;
- a non-zero agent exit;
- each verifier problem, translated into what it means ("money moved for a
  transfer differs from its amount");
- a final message that claims success the verifier does not confirm;
- a fatal error turned into a skip;
- integrity: edits to protected files, a removed crash point the verifier
  arms, or network inside the sandbox.

An attempt that passes but touched the grader is `INVALID`, not `RESOLVED`.
Example reports: `results/traces/stub-cheat/*.audit.md`.

## Findings in the upstream projects

- **events-lakehouse: concurrent recovery of one batch's Parquet file.** At
  startup every worker rewrites the Parquet file of each committed batch that
  has none (`recover_parquet`). Two workers started together after a crash
  both do it for the same batch, through the same `.tmp` name, and one of
  them fails with `FileNotFoundError` on `os.replace`. Nothing is lost: the
  winner's file is complete and the loser exits non-zero. The verifier's first
  draft hit this on the reference code. The lakehouse scenario now restarts
  the crashed worker alone first, because this race is not what the tasks
  test. Fixed upstream in
  [e382864](https://github.com/chipontix/events-lakehouse/commit/e382864)
  (a unique tmp name via `mkstemp`, with a regression test). The tasks here
  stay pinned to the commit before it.

## Infrastructure

**Requirement of the stand: Kafka runs on the host JVM** (`make up
KAFKA=native`, Java 17+ on the host). `infra/compose.yaml` can run it in
Docker too, and CI does that, but on a shared Docker VM it is not reliable
enough for verification (below). Ports are 23000+: Postgres 23432, Kafka
23092 (controller 23093), the ledger crash harness 23100+. The stand takes
`SAB_PG_PORT` and `SAB_KAFKA_PORT`; the verifiers take `SAB_PG_URL`,
`SAB_KAFKA` and `SAB_PORT_BASE`. On a machine where several projects share
one Docker VM, bring the stand up only while you hold the shared queue lock,
and bring it down afterwards.

`infra/compose.yaml` runs Postgres 17.6 (logical decoding on) and Kafka 3.9.1
(KRaft, single node), with the same image tags the upstream projects use, so
nothing extra is pulled where they are already present. The Docker VM on the
machine these results come from (colima, 4 vCPU) was at times shared with
several other projects' brokers and builds. Its load average then reached
174, and a broker inside it timed out on heartbeats to its own controller
(`results/raw/environment.txt`). `make up KAFKA=native`
(`infra/kafka-native.sh`) instead runs the same Kafka on the host JVM, from
the official tarball checked against its SHA-512. All the results here were
recorded that way. The agent container is the only thing that has to be in
Docker.

Disk: the bench pulls one image of its own (`postgres:17.6-alpine`; the Kafka
image is the upstream projects' and the agent image is `python:3.13-slim`) and
keeps its caches under `.cache/` (Go build cache, Kafka, the lakehouse
verifier's virtualenv). The sizes are in `results/raw/environment.txt`, and
`make clean` removes the caches.

## Limitations and what is not done

- **The agents are stubs.** No model was run against these tasks, and the
  pass@k numbers come from stubs whose outcome is known in advance. The
  "minimal agent on a local model" from the plan was not built.
- **The agent container has no toolchain.** `python:3.13-slim` is enough to
  play back a patch. A real agent that should build Go and run the unit tests
  offline needs an image with Go and a module cache, an estimated 1 GB or
  more, which the disk budget did not allow.
- **Six tasks, three projects.** The userver task was dropped for disk
  (above).
- **A verifier only sees the interleavings its scenario reaches.** The
  survivors above are the measured blind spots. The lakehouse scenario lets
  workers take turns and never makes two of them own a partition at once.
  With two owners, each re-read message would be counted in
  `ingest_batches.duplicates` even by the original code, so the duplicate
  invariant would need a different reference. That follows from the code; it
  was not run.
- **Budgets and timings come from a loaded laptop** (see the load average in
  each result file). They are pass/fail thresholds, not benchmarks.
- **Local credentials only.** `postgres:postgres` in `infra/` and in the
  verifiers' defaults is the throwaway database of the test brokers.

## Layout

```
tasks/<id>/        task.json, issue.md, bug/fix/wrong/cheat.patch, near_misses/
verifiers/         ledger (crash harness wrapper), pgsync (sabcheck.go), lakehouse (verify.py)
bench/             workspace (snapshots, protected paths), verify, run (container, trace, pass@k),
                   mutate, audit, cli
agents/stub.py     the stub agent (stdlib only, runs in the container)
infra/             compose file for the brokers, native Kafka script
scripts/           author_patches.py (writes the patches), render_readme.py (fills the tables above)
results/           every number in this README, raw
```
