# Duplicate counts too high after an ingest worker crash

An ingest worker was killed (OOM) and restarted. Afterwards `ingest_batches`
has a batch with `inserted = 0` whose messages were all counted as
duplicates, so the sum of `duplicates` is larger than the number of messages
the producer re-sent and `lakehouse verify` fails on the duplicate count.
The events themselves are not duplicated, in Postgres or in the lake.

Expected: a crash at any point leaves Postgres, the lake and the batch
statistics exactly as if the worker had never crashed.


## Your environment

The repository is checked out in `/workspace`. There is no network: Postgres
and Kafka are not reachable from your sandbox, and nothing can be downloaded.
Change the code so the problem is fixed. Your change is verified outside the
sandbox by running the real services under crashes, restarts and load and
checking the system's invariants against an independent reference; the
harness and checker files are restored to their original version before that
run, so changing them has no effect.
