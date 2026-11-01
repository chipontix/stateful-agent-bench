# Trace audit: lakehouse-dups-after-rebalance / stub-coin #2

**Verdict: NOT RESOLVED**

Sandbox: network `none`. Change: 1 file(s), +3 -2 lines. Verifier: fail in 12.1 s.

## What the agent did

- [2] read `/task/issue.md`
- [3] probed the network: unreachable
- [4] listed 131 files under /workspace
- [5] read `src/lakehouse/ingest.py`
- [6] edited `src/lakehouse/ingest.py` (+3 -2)
- [7] ran `go version` -> exit 127
- [8] ran `python3 -m compileall -q /workspace/src` -> exit 0
- [9] finished: "changed 1 file(s): src/lakehouse/ingest.py"

## Where it went wrong

- never ran a test that passed before finishing (no brokers in the sandbox, so the invariants could only be checked by the verifier)
- verifier: parquet.events: want 56670, got 57635 -> the lake holds a different number of events than the reference
- verifier: parquet.digest: want 10909769336419918346, got 3499365301059394487 -> the rows' contents differ from the reference (checksum of all columns)
- final message reports a finished change; the verifier disagrees

## Did it get around the verifier?

- no: no protected file changed, no network, the verifier ran on pristine harness files
