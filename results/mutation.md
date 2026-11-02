| task | mutants | stillborn | killed | survived | caught | near misses killed |
|---|---|---|---|---|---|---|
| lakehouse-dups-after-rebalance | 7 | 0 | 6 | 1 | 86% | 0/1 |
| lakehouse-offsets-after-parquet | 6 | 2 | 3 | 1 | 75% | – |
| ledger-hold-release-race | 4 | 1 | 2 | 1 | 67% | – |
| ledger-retry-double-charge | 12 | 3 | 7 | 2 | 78% | – |
| pgsync-deletes-replica-identity | 4 | 0 | 2 | 2 | 50% | 1/1 |
| pgsync-slot-dropped-on-restart | 4 | 4 | 0 | 0 | n/a | 0/1 |
| miniserve-preempt-frees-shared-block | 3 | 0 | 2 | 1 | 67% | – |

Survivors:

- `lakehouse-dups-after-rebalance` m02 (revert-hunk): src/lakehouse/ingest.py:105 hunk reverted to the buggy version
- `lakehouse-dups-after-rebalance` reload-seen-at-start (near-miss): tasks/lakehouse-dups-after-rebalance/near_misses/reload-seen-at-start.patch
- `lakehouse-offsets-after-parquet` m02 (revert-hunk): src/lakehouse/ingest.py:167 hunk reverted to the buggy version
- `ledger-hold-release-race` m04 (delete-line): internal/ledger/store.go:40 deleted `return "", err`
- `ledger-retry-double-charge` m01 (revert-hunk): internal/db/schema.sql:68 hunk reverted to the buggy version
- `ledger-retry-double-charge` m04 (delete-line): internal/db/schema.sql:71 deleted `at         timestamptz NOT NULL DEFAULT now(),`
- `pgsync-deletes-replica-identity` m01 (revert-hunk): deploy/postgres/init.sql:28 hunk reverted to the buggy version
- `pgsync-deletes-replica-identity` m03 (delete-line): deploy/postgres/init.sql:33 deleted `ALTER TABLE products REPLICA IDENTITY FULL;`
- `pgsync-slot-dropped-on-restart` drop-on-shutdown (near-miss): tasks/pgsync-slot-dropped-on-restart/near_misses/drop-on-shutdown.patch
- `miniserve-preempt-frees-shared-block` m01 (revert-hunk): mini_serve/blocks.py:60 hunk reverted to the buggy version
