# Trace audit: pgsync-slot-dropped-on-restart / stub-coin #4

**Verdict: NOT RESOLVED**

Sandbox: network `none`. Change: 1 file(s), +4 -2 lines. Verifier: fail in 70.8 s.

## What the agent did

- [2] read `/task/issue.md`
- [3] probed the network: unreachable
- [4] listed 194 files under /workspace
- [5] read `internal/pipeline/cdc.go`
- [6] edited `internal/pipeline/cdc.go` (+4 -2)
- [7] ran `go version` -> exit 127
- [8] finished: "changed 1 file(s): internal/pipeline/cdc.go"

## Where it went wrong

- never ran a test that passed before finishing (no brokers in the sandbox, so the invariants could only be checked by the verifier)
- verifier: 31 missing, 58 stale, 29 extra documents -> documents in the table are not (or no longer) searchable
- verifier: id 1137: index has version 8630, table 10870 -> invariant violated
- verifier: id 413: index has version 6892, table 7968 -> invariant violated
- verifier: id 893: index has version 6932, table 7658 -> invariant violated
- verifier: id 256: index has version 6774, table 8078 -> invariant violated
- verifier: id 771: index has version 6836, table 7828 -> invariant violated
- final message reports a finished change; the verifier disagrees

## Did it get around the verifier?

- no: no protected file changed, no network, the verifier ran on pristine harness files
