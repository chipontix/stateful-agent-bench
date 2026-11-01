# cdc crash-loops after the first product delete

Since the latest release the `cdc` connector exits as soon as a product is
deleted:

    {"level":"ERROR","msg":"decode row","table":"products","type":"DELETE","error":"version: unexpected <nil>"}

It exits the same way after every restart, so nothing written after the
delete reaches search. Inserts and updates on their own work.

Expected: deletes become tombstones in the index as before, a product that is
deleted and later inserted again with the same id is searchable again, and
the connector keeps running.


## Your environment

The repository is checked out in `/workspace`. There is no network: Postgres
and Kafka are not reachable from your sandbox, and nothing can be downloaded.
Change the code so the problem is fixed. Your change is verified outside the
sandbox by running the real services under crashes, restarts and load and
checking the system's invariants against an independent reference; the
harness and checker files are restored to their original version before that
run, so changing them has no effect.
