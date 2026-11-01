# Trace audit: lakehouse-dups-after-rebalance / stub-coin #4

**Verdict: NOT RESOLVED**

Sandbox: network `none`. Change: 1 file(s), +3 -2 lines. Verifier: fail in 19.0 s.

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
- verifier: parquet.events: want 56727, got 57643 -> the lake holds a different number of events than the reference
- verifier: parquet.digest: want 3055371198571843921, got 10258328271677246779 -> invariant violated
- final message reports a finished change; the verifier disagrees

## Did it get around the verifier?

- no: no protected file changed, no network, the verifier ran on pristine harness files
