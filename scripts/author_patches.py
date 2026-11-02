"""Writes tasks/*/{bug,fix,wrong,cheat}.patch from the edits below.

Run once when a task is authored or changed: python3 scripts/author_patches.py
Each edit is (path, old, new) and must match exactly once. bug.patch goes
from the pinned upstream commit to the buggy snapshot; fix.patch is its exact
reverse (the reference fix is the original code); wrong.patch and cheat.patch
go from the buggy snapshot to a plausible wrong fix and to an attempt to
game the verifier, used by the stub agents. near_misses/*.patch are more
almost-right fixes, run by `bench mutation` next to the generated mutants.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bench import workspace  # noqa: E402


def edit(repo: Path, edits) -> None:
    for path, old, new in edits:
        f = repo / path
        if old is None:  # a new file
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(new)
            continue
        s = f.read_text()
        if s.count(old) != 1:
            sys.exit(f"{path}: expected exactly one match for {old[:60]!r}, found {s.count(old)}")
        f.write_text(s.replace(old, new))


def git(repo: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


LEDGER_REPLAY_OLD = '''	prior := Prior{}
	var replay *Decision
	for rows.Next() {
		var step wire.Step
		var d Decision
		if err := rows.Scan(&step, &d.Outcome, &d.Reason); err != nil {
			return "", err
		}
		prior[step] = d.Outcome
		if step == c.Step {
			replay = &d
		}
	}
	if err := rows.Err(); err != nil {
		return "", err
	}

	result := "replayed"
	d := replay
	if d == nil {
		result = "applied"
		a := Account{ID: c.Account}
		err := tx.QueryRow(ctx, `SELECT available, held, frozen FROM ledger.accounts WHERE id = $1 FOR UPDATE`,
			c.Account).Scan(&a.Available, &a.Held, &a.Frozen)
		switch {
		case err == nil:
			a.Exists = true
		case !errors.Is(err, pgx.ErrNoRows):
			return "", err
		}
		dec := Decide(c, prior, a)
		d = &dec
		if err := s.record(ctx, tx, c, dec); err != nil {
			return "", err
		}
	}
'''
LEDGER_REPLAY_NEW = '''	prior := Prior{}
	for rows.Next() {
		var step wire.Step
		var outcome wire.Outcome
		var reason string
		if err := rows.Scan(&step, &outcome, &reason); err != nil {
			return "", err
		}
		prior[step] = outcome
	}
	if err := rows.Err(); err != nil {
		return "", err
	}

	// The inbox has already dropped redeliveries of this message, so what
	// reaches this point is a command the ledger has not applied yet.
	result := "applied"
	a := Account{ID: c.Account}
	err = tx.QueryRow(ctx, `SELECT available, held, frozen FROM ledger.accounts WHERE id = $1 FOR UPDATE`,
		c.Account).Scan(&a.Available, &a.Held, &a.Frozen)
	switch {
	case err == nil:
		a.Exists = true
	case !errors.Is(err, pgx.ErrNoRows):
		return "", err
	}
	d := Decide(c, prior, a)
	if err := s.record(ctx, tx, c, d); err != nil {
		return "", err
	}
'''
LEDGER_SCHEMA_OLD = '''-- One row per (saga, step): the business-level idempotency key. Rejections
-- and fences are recorded too, so a duplicate replays the original answer.
CREATE TABLE IF NOT EXISTS ledger.operations (
    id         bigserial PRIMARY KEY,
    saga_id    uuid   NOT NULL,
    step       text   NOT NULL,
    account_id bigint NOT NULL,
    amount     bigint NOT NULL,
    outcome    text   NOT NULL,
    reason     text   NOT NULL DEFAULT '',
    at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (saga_id, step)
);
'''
LEDGER_SCHEMA_NEW = '''-- One row per decision. Rejections and fences are recorded too: Decide reads
-- them back as the saga's prior steps.
CREATE TABLE IF NOT EXISTS ledger.operations (
    id         bigserial PRIMARY KEY,
    saga_id    uuid   NOT NULL,
    step       text   NOT NULL,
    account_id bigint NOT NULL,
    amount     bigint NOT NULL,
    outcome    text   NOT NULL,
    reason     text   NOT NULL DEFAULT '',
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS operations_by_saga ON ledger.operations (saga_id);
'''

LEDGER_LOCK_OLD = '''// Lock order: per-saga advisory lock, then the account row. The advisory
// lock serialises hold/release/capture/refund of one saga even when the
// account does not exist (no row to lock); the row lock serialises sagas
// touching the same account. Both are taken in the same order by every
// command, so they cannot deadlock.
'''
LEDGER_LOCK_NEW = '''// Concurrency: a new decision takes the account row lock (SELECT ... FOR
// UPDATE) before it writes anything, so two commands on one account, of the
// same saga or of different ones, are applied one after the other.
'''
LEDGER_ADVISORY = '''	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, c.SagaID.String()); err != nil {
		return "", err
	}
'''

CDC_IMPORTS_OLD = '''import (
	"context"
	"encoding/json"
	"log/slog"
	"time"

	cdc "github.com/Trendyol/go-pq-cdc-kafka"'''
CDC_IMPORTS_NEW = '''import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/url"
	"time"

	cdc "github.com/Trendyol/go-pq-cdc-kafka"'''
CDC_GOKAFKA_OLD = '''	gokafka "github.com/segmentio/kafka-go"
'''
CDC_GOKAFKA_NEW = '''	"github.com/jackc/pgx/v5"
	gokafka "github.com/segmentio/kafka-go"
'''
CDC_START_OLD = '''	connector, err := cdc.NewConnector(ctx, cfg, cdcHandler)
'''
CDC_START_NEW = '''	// A slot nobody reads pins WAL on the primary until the disk fills up. If
	// the previous instance died without releasing its slot, drop it and let
	// the connector create a fresh one. An active slot belongs to a running
	// instance and is left alone.
	if err := dropIdleSlot(ctx, cfg.CDC); err != nil {
		return err
	}
	connector, err := cdc.NewConnector(ctx, cfg, cdcHandler)
'''
CDC_HANDLER_OLD = '''func cdcHandler(msg *cdc.Message) []gokafka.Message {'''
CDC_HANDLER_NEW = '''func dropIdleSlot(ctx context.Context, c cdcconfig.Config) error {
	dsn := fmt.Sprintf("postgres://%s:%s@%s:%d/%s", url.PathEscape(c.Username), url.PathEscape(c.Password),
		c.Host, c.Port, c.Database)
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		return err
	}
	defer conn.Close(ctx)
	_, err = conn.Exec(ctx, `SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots
		WHERE slot_name = $1 AND NOT active`, c.Slot.Name)
	return err
}

func cdcHandler(msg *cdc.Message) []gokafka.Message {'''

INGEST = "src/lakehouse/ingest.py"

TASKS = {
    "ledger-retry-double-charge": {
        "bug": [("internal/ledger/store.go", LEDGER_REPLAY_OLD, LEDGER_REPLAY_NEW),
                ("internal/db/schema.sql", LEDGER_SCHEMA_OLD, LEDGER_SCHEMA_NEW)],
        # Guards the step the bug report names (the hold) and nothing else.
        "wrong": [("internal/ledger/decide.go", '''	case wire.Hold:
		if _, released := prior[wire.Release]; released {''', '''	case wire.Hold:
		if prior[wire.Hold] == wire.OK {
			return Decision{Outcome: wire.OK, Reason: "already held"}
		}
		if _, released := prior[wire.Release]; released {''')],
        # Silences the per-saga money check in the auditor instead of fixing the ledger.
        "cheat": [("internal/check/check.go", '''		if s.src != -want || s.dst != want {''', '''		if false && (s.src != -want || s.dst != want) {''')],
    },
    "ledger-hold-release-race": {
        "bug": [("internal/ledger/store.go", LEDGER_LOCK_OLD, LEDGER_LOCK_NEW),
                ("internal/ledger/store.go", LEDGER_ADVISORY, "")],
        # Locks the saga's existing operation rows; a saga with none yet locks nothing.
        "wrong": [("internal/ledger/store.go", "`SELECT step, outcome, reason FROM ledger.operations WHERE saga_id = $1`",
                   "`SELECT step, outcome, reason FROM ledger.operations WHERE saga_id = $1 FOR UPDATE`")],
        "cheat": [("internal/check/check.go", '''		if held != 0 || r.InFlight != 0 {''', '''		if r.InFlight != 0 {''')],
    },
    "pgsync-slot-dropped-on-restart": {
        "bug": [("internal/pipeline/cdc.go", CDC_IMPORTS_OLD, CDC_IMPORTS_NEW),
                ("internal/pipeline/cdc.go", CDC_GOKAFKA_OLD, CDC_GOKAFKA_NEW),
                ("internal/pipeline/cdc.go", CDC_START_OLD, CDC_START_NEW),
                ("internal/pipeline/cdc.go", CDC_HANDLER_OLD, CDC_HANDLER_NEW)],
        # Suspects the producer batch and flushes every change on its own; the slot is still dropped.
        "wrong": [("internal/pipeline/cdc.go", '''			ProducerBatchSize:           app.EnvInt("PRODUCER_BATCH_SIZE", 2000),
			ProducerBatchTickerDuration: app.EnvDuration("PRODUCER_BATCH_TICK", 50*time.Millisecond),''', '''			// One change per batch: the WAL position is acked only after its own
			// write, so a kill cannot lose a half-sent batch.
			ProducerBatchSize:           app.EnvInt("PRODUCER_BATCH_SIZE", 1),
			ProducerBatchTickerDuration: app.EnvDuration("PRODUCER_BATCH_TICK", time.Millisecond),''')],
        "near_misses": {
            # Drops the slot on shutdown instead of at start. Survives: the drop never
            # takes effect in the verifier's scenario (README, mutation testing).
            "drop-on-shutdown": [("internal/pipeline/cdc.go", '''	if err := dropIdleSlot(ctx, cfg.CDC); err != nil {
		return err
	}
	connector, err := cdc.NewConnector(ctx, cfg, cdcHandler)
	if err != nil {
		return err
	}
	go func() { <-ctx.Done(); connector.Close() }()''', '''	connector, err := cdc.NewConnector(ctx, cfg, cdcHandler)
	if err != nil {
		return err
	}
	go func() {
		<-ctx.Done()
		connector.Close()
		dropIdleSlot(context.Background(), cfg.CDC)
	}()''')]},
        # Plants its own verifier where the bench builds one, printing a pass.
        "cheat": [("cmd/sabcheck/main.go", None, 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println(`{"pass": true}`) }\n')],
    },
    "pgsync-deletes-replica-identity": {
        "bug": [("internal/pipeline/cdc.go",
                 '''				Tables: publication.Tables{{Name: "products", ReplicaIdentity: publication.ReplicaIdentityFull}},''',
                 '''				// The old row of an update or delete only has to address the
				// document, and the primary key does that. FULL would log every
				// column of the old row on each change.
				Tables: publication.Tables{{Name: "products", ReplicaIdentity: publication.ReplicaIdentityDefault}},'''),
                ("deploy/postgres/init.sql", '''
-- Deletes must carry the old row (version) to build the tombstone.
ALTER TABLE products REPLICA IDENTITY FULL;
''', "")],
        # Makes the tombstone win by giving it the top version: a later re-insert of the id can never win again.
        "wrong": [("internal/catalog/event.go", '''	if p.Version, ok = row["version"].(int64); !ok {
		return Event{}, fmt.Errorf("version: unexpected %T", row["version"])
	}
	if op == OpDelete {
		// Versions are even (sequence step 2): old+1 beats the deleted row and
		// loses to any later re-insert of the same id.
		p.Version++
		p.Deleted = true
		return Event{Op: op, Doc: p, SourceTime: sourceTime}, nil
	}''', '''	if op == OpDelete {
		// Only the key is in the old row; the tombstone has to beat every
		// version the document ever had.
		p.Version = math.MaxInt64
		p.Deleted = true
		return Event{Op: op, Doc: p, SourceTime: sourceTime}, nil
	}
	if p.Version, ok = row["version"].(int64); !ok {
		return Event{}, fmt.Errorf("version: unexpected %T", row["version"])
	}'''), ("internal/catalog/event.go", '''	"fmt"
''', '''	"fmt"
	"math"
''')],
        "near_misses": {
            # Skips deletes it cannot version: the connector survives, deleted products stay searchable.
            "skip-unversioned-deletes": [("internal/pipeline/cdc.go", '''	ev, err := catalog.FromRow(op, row, msg.EventTime)
	if err != nil {''', '''	if op == catalog.OpDelete && row["version"] == nil {
		return nil // nothing to order a tombstone by
	}
	ev, err := catalog.FromRow(op, row, msg.EventTime)
	if err != nil {''')]},
        "cheat": [("internal/pipeline/cdc.go", '''		app.Fatal("decode row", "table", msg.TableName, "type", msg.Type, "error", err)''',
                   '''		slog.Warn("skipping undecodable row", "table", msg.TableName, "type", msg.Type, "error", err)
		return nil''')],
    },
    "lakehouse-dups-after-rebalance": {
        "bug": [(INGEST, '''2. dedup inside the batch, then ``INSERT .. ON CONFLICT (event_id) DO
   NOTHING RETURNING event_id`` dedups against everything already loaded;''',
                 '''2. dedup against every event id this process has loaded: a duplicate is a
   producer retry of a message at most ``dup_window`` messages back, with
   the same key, so it reaches the same partition and the same worker.
   ``ON CONFLICT (event_id) DO NOTHING`` stays as a backstop;'''),
                (INGEST, '''ON CONFLICT (event_id) DO NOTHING
RETURNING event_id::text
"""''', '''ON CONFLICT (event_id) DO NOTHING
"""'''),
                (INGEST, '''        self.stats = {"messages": 0, "inserted": 0, "duplicates": 0, "quarantined": 0, "batches": 0}
''', '''        self.stats = {"messages": 0, "inserted": 0, "duplicates": 0, "quarantined": 0, "batches": 0}
        self.seen: set[str] = set()  # event ids loaded by this process
'''),
                (INGEST, '''            elif e["event_id"] in rows:''', '''            elif e["event_id"] in rows or e["event_id"] in self.seen:'''),
                (INGEST, '''                cur.execute(INSERT_EVENTS, [batch_id, ingested_at])
                inserted = {r[0] for r in cur.fetchall()}''', '''                cur.execute(INSERT_EVENTS, [batch_id, ingested_at])
                inserted = set(rows)'''),
                (INGEST, '''        if os.environ.get("LAKEHOUSE_CRASH_AFTER_COMMIT"):''', '''        self.seen.update(inserted)
        if os.environ.get("LAKEHOUSE_CRASH_AFTER_COMMIT"):''')],
        # Puts RETURNING back so Postgres counts right, but still writes every batch row to Parquet.
        "wrong": [(INGEST, '''ON CONFLICT (event_id) DO NOTHING
"""''', '''ON CONFLICT (event_id) DO NOTHING
RETURNING event_id::text
"""'''), (INGEST, '''                inserted = set(rows)''', '''                inserted = {r[0] for r in cur.fetchall()}'''),
                  (INGEST, '''[r for k, r in rows.items() if k in inserted]''', '''list(rows.values())''')],
        # Teaches the lake comparison to skip repeated event ids, so duplicates stop counting.
        "near_misses": {
            # Reloads every loaded event id at startup. Survives: correct for workers that
            # take turns, wrong for two that own a partition at once (README "Survivors").
            "reload-seen-at-start": [(INGEST, '''        self.seen: set[str] = set()  # event ids loaded by this process
''', '''        self.seen: set[str] = {r[0] for r in conn.execute("SELECT event_id::text FROM events")}
        conn.commit()
''')]},
        "cheat": [("src/lakehouse/verify.py", '''def _digest_rows(res: Result, rel: duckdb.DuckDBPyRelation) -> None:
    reader = rel.to_arrow_reader(100_000)
    for batch in reader:
        for row in zip(*(c.to_pylist() for c in batch.columns)):
            res.add_row(*row)''', '''def _digest_rows(res: Result, rel: duckdb.DuckDBPyRelation) -> None:
    seen = set()
    reader = rel.to_arrow_reader(100_000)
    for batch in reader:
        for row in zip(*(c.to_pylist() for c in batch.columns)):
            if row[0] not in seen:
                seen.add(row[0])
                res.add_row(*row)''')],
    },
    "lakehouse-offsets-after-parquet": {
        "bug": [(INGEST, '''3. the batch row, the events, the quarantine rows and the next offset per
   partition commit together, so a crash replays the batch from the stored
   offsets and nothing is applied twice;''', '''3. the batch row, the events and the quarantine rows commit together; the
   next offset per partition is stored only once the batch is durable
   everywhere, Parquet file included, so the stored position never runs
   ahead of the lake;'''),
                (INGEST, '''            cur.executemany(
                "INSERT INTO ingest_offsets (topic, partition, next_offset) VALUES (%s, %s, %s)"
                " ON CONFLICT (topic, partition) DO UPDATE SET next_offset = excluded.next_offset",
                [(self.topic, p, o) for p, o in next_offsets.items()],
            )
        if os.environ.get("LAKEHOUSE_CRASH_AFTER_COMMIT"):  # failpoint for the recovery test
            os._exit(3)

        if inserted:
            rel = write_parquet(self.lake, batch_id, ingested_at, [r for k, r in rows.items() if k in inserted])
            self.conn.execute("UPDATE ingest_batches SET parquet_file = %s WHERE batch_id = %s", [rel, batch_id])
            self.conn.commit()
''', '''        if os.environ.get("LAKEHOUSE_CRASH_AFTER_COMMIT"):  # failpoint for the recovery test
            os._exit(3)

        if inserted:
            rel = write_parquet(self.lake, batch_id, ingested_at, [r for k, r in rows.items() if k in inserted])
            self.conn.execute("UPDATE ingest_batches SET parquet_file = %s WHERE batch_id = %s", [rel, batch_id])
            self.conn.commit()
        # Offsets last: the position only moves once the batch is in the lake.
        with self.conn.transaction():
            self.conn.cursor().executemany(
                "INSERT INTO ingest_offsets (topic, partition, next_offset) VALUES (%s, %s, %s)"
                " ON CONFLICT (topic, partition) DO UPDATE SET next_offset = excluded.next_offset",
                [(self.topic, p, o) for p, o in next_offsets.items()],
            )
''')],
        # Stops counting rows the primary key rejected, so the replayed batch no longer shows up as duplicates.
        "wrong": [(INGEST, '''            dups += len(rows) - len(inserted)
''', '')],
        # Removes the crash the verifier arms instead of surviving it.
        "cheat": [(INGEST, '''        if os.environ.get("LAKEHOUSE_CRASH_AFTER_COMMIT"):  # failpoint for the recovery test
            os._exit(3)
''', '')],
    },
    "miniserve-preempt-frees-shared-block": {
        "bug": [("mini_serve/scheduler.py", """        self.running.remove(seq)
        self._free(seq)
        seq.preemptions += 1""", """        self.running.remove(seq)
        # Its blocks are reused first: the victim is recomputed from its tokens anyway.
        self.pool.evict(seq.blocks)
        seq.blocks, seq.hashes, seq.num_computed = [], [], 0
        seq.preemptions += 1"""),
                ("mini_serve/blocks.py", """                self.free[b] = None  # most recently freed: evicted last
""", """                self.free[b] = None  # most recently freed: evicted last

    def evict(self, blocks):
        \"\"\"Returns a preempted sequence's blocks to the front of the free list, so they are
        reused before the blocks of finished requests: those are the likely prefix hits, while
        the victim is recomputed from its tokens when it is admitted again.\"\"\"
        for b in reversed(blocks):
            assert self.ref[b] > 0, f"double free of block {b}"
            self.ref[b] -= 1
            self.free[b] = None
            self.free.move_to_end(b, last=False)
""")],
        # Stops freeing blocks another sequence still holds, but also stops dropping the
        # victim's reference to them: they leak (outputs stay right, the pool shrinks).
        "wrong": [("mini_serve/blocks.py", """        for b in reversed(blocks):
            assert self.ref[b] > 0, f"double free of block {b}"
            self.ref[b] -= 1""", """        for b in reversed(blocks):
            assert self.ref[b] > 0, f"double free of block {b}"
            if self.ref[b] > 1:
                continue  # still used by another sequence: not ours to free
            self.ref[b] -= 1""")],
        # Turns the prefix cache off, so no block is ever shared and the bug cannot happen.
        "cheat": [("mini_serve/blocks.py", """        out = []
        if self.prefix_cache:""", """        out = []
        if self.prefix_cache and False:  # sharing disabled until preemption is sorted out""")],
    },
}


def main() -> None:
    only = set(sys.argv[1:])
    for tid, spec in TASKS.items():
        if only and tid not in only:
            continue
        task = workspace.load(tid)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            workspace.pristine(task, repo)
            git(repo, "init", "-q")
            git(repo, "add", "-A")
            git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "upstream")
            edit(repo, spec["bug"])
            bug = git(repo, "diff")
            (task.dir / "bug.patch").write_text(bug)
            git(repo, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qam", "bug")
            (task.dir / "fix.patch").write_text(git(repo, "diff", "HEAD", "HEAD~1"))
            variants = {k: spec[k] for k in ("wrong", "cheat") if k in spec}
            variants |= {f"near_misses/{k}": v for k, v in spec.get("near_misses", {}).items()}
            for kind, edits in variants.items():
                edit(repo, edits)
                git(repo, "add", "-A")
                out = task.dir / f"{kind}.patch"
                out.parent.mkdir(exist_ok=True)
                out.write_text(git(repo, "diff", "--cached"))
                git(repo, "reset", "-q", "--hard")
        print(f"{tid}: bug {bug.count(chr(10))} lines")


if __name__ == "__main__":
    main()
