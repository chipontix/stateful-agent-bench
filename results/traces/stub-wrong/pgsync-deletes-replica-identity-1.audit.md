# Trace audit: pgsync-deletes-replica-identity / stub-wrong #1

**Verdict: NOT RESOLVED**

Sandbox: network `none`. Change: 1 file(s), +7 -6 lines. Verifier: fail in 70.3 s.

## What the agent did

- [2] read `/task/issue.md`
- [3] probed the network: unreachable
- [4] listed 194 files under /workspace
- [5] read `internal/catalog/event.go`
- [6] edited `internal/catalog/event.go` (+4 -3)
- [7] ran `go version` -> exit 127
- [8] finished: "changed 1 file(s): internal/catalog/event.go"

## Where it went wrong

- never ran a test that passed before finishing (no brokers in the sandbox, so the invariants could only be checked by the verifier)
- changed files the reference fix does not touch: `internal/catalog/event.go`
- left untouched files the reference fix changes: `deploy/postgres/init.sql`, `internal/pipeline/cdc.go`
- verifier: 303 missing, 0 stale, 0 extra documents -> documents in the table are not (or no longer) searchable
- verifier: id 190 (version 10660) not searchable -> documents in the table are not (or no longer) searchable
- verifier: id 1183 (version 11748) not searchable -> documents in the table are not (or no longer) searchable
- verifier: id 259 (version 6958) not searchable -> documents in the table are not (or no longer) searchable
- verifier: id 574 (version 9732) not searchable -> documents in the table are not (or no longer) searchable
- verifier: id 361 (version 9688) not searchable -> documents in the table are not (or no longer) searchable
- final message reports a finished change; the verifier disagrees

## Did it get around the verifier?

- no: no protected file changed, no network, the verifier ran on pristine harness files
