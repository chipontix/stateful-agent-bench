# Customers charged twice after the ledger was slow

Since this week's release, support has tickets from customers whose account
lost twice the amount of a single transfer, and from recipients who got paid
twice. The transfer itself shows up once and ends `COMPLETED`. It happens
more when the ledger consumer is slow or restarts under load (the
orchestrator times out and sends the step again), and not at all on a quiet
system.

The auditor agrees: `ledgersaga check -quiescent` after such a period
reports sagas that "moved src -2x dst 2x, want -x/x" and ledger effects that
the saga's final state does not allow.

Expected: every transfer moves exactly its amount once, whatever the delivery
order, retries, duplicates or crashes.


## Your environment

The repository is checked out in `/workspace`. There is no network: Postgres
and Kafka are not reachable from your sandbox, and nothing can be downloaded.
Change the code so the problem is fixed. Your change is verified outside the
sandbox by running the real services under crashes, restarts and load and
checking the system's invariants against an independent reference; the
harness and checker files are restored to their original version before that
run, so changing them has no effect.
