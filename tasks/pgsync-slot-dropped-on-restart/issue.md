# Changes made while cdc restarts never reach search

When the `cdc` connector restarts (a deploy, or the pod being OOM-killed),
the products created, updated or deleted while it was down are missing or
stale in search for good. `reconcile` reports them as missing or stale
afterwards. The connector logs nothing unusual, and changes made after it is
back flow normally. Before the latest release restarts were harmless: the
connector caught up from where it stopped.

Expected: no committed change is lost, however and whenever the connector is
stopped or killed.


## Your environment

The repository is checked out in `/workspace`. There is no network: Postgres
and Kafka are not reachable from your sandbox, and nothing can be downloaded.
Change the code so the problem is fixed. Your change is verified outside the
sandbox by running the real services under crashes, restarts and load and
checking the system's invariants against an independent reference; the
harness and checker files are restored to their original version before that
run, so changing them has no effect.
