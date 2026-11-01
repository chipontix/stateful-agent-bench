# Money stays held after cancelled transfers

After the ledger was down for a few minutes and then worked through its
backlog, some accounts have part of their balance stuck in `held`. Each of
them belongs to a transfer that ended `CANCELLED` ("hold timed out"), so the
money should be back in `available`. Nothing releases it later, and the
customer cannot spend it.

`ledgersaga check -quiescent` reports "at rest but N held and 0 in clearing";
the journal itself still balances.

Expected: once every saga is final, nothing is held and every cancelled
transfer has left the source account as it was.


## Your environment

The repository is checked out in `/workspace`. There is no network: Postgres
and Kafka are not reachable from your sandbox, and nothing can be downloaded.
Change the code so the problem is fixed. Your change is verified outside the
sandbox by running the real services under crashes, restarts and load and
checking the system's invariants against an independent reference; the
harness and checker files are restored to their original version before that
run, so changing them has no effect.
