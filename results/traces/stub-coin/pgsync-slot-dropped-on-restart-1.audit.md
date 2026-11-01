# Trace audit: pgsync-slot-dropped-on-restart / stub-coin #1

**Verdict: NOT RESOLVED**

Sandbox: network `none`. Change: 1 file(s), +4 -2 lines. Verifier: fail in 76.3 s.

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
- verifier: 31 missing, 70 stale, 43 extra documents -> documents in the table are not (or no longer) searchable
- verifier: id 865: index has version 6524, table 7646 -> invariant violated
- verifier: id 1331: index has version 10194, table 10902 -> invariant violated
- verifier: id 1058 (version 7896) not searchable -> invariant violated
- verifier: id 181: index has version 7360, table 8106 -> invariant violated
- verifier: id 580: index has version 8618, table 10918 -> invariant violated
- final message reports a finished change; the verifier disagrees

## Did it get around the verifier?

- no: no protected file changed, no network, the verifier ran on pristine harness files
