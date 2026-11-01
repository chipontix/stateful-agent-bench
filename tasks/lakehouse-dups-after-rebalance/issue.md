# Duplicate events in the lake after partitions moved between workers

We moved partitions between ingest workers (scaled from two workers to three
and back, restarting them). Since then the Parquet lake, and `fct_events`
built from it, contain some `event_id`s twice, and the sum of
`ingest_batches.duplicates` is lower than the number of messages the
producer actually re-sent. The `events` table in Postgres looks right. A
worker that keeps its partitions and is never restarted produces correct
output.

Expected: every event exactly once in Postgres and in the lake, and the
duplicate count equal to the number of re-sent messages, however partitions
are assigned to workers and however often workers restart.


## Your environment

The repository is checked out in `/workspace`. There is no network: Postgres
and Kafka are not reachable from your sandbox, and nothing can be downloaded.
Change the code so the problem is fixed. Your change is verified outside the
sandbox by running the real services under crashes, restarts and load and
checking the system's invariants against an independent reference; the
harness and checker files are restored to their original version before that
run, so changing them has no effect.
