# Trace audit: ledger-hold-release-race / stub-wrong #1

**Verdict: NOT RESOLVED**

Sandbox: network `none`. Change: 1 file(s), +1 -1 lines. Verifier: fail in 41.8 s.

## What the agent did

- [2] read `/task/issue.md`
- [3] probed the network: unreachable
- [4] listed 214 files under /workspace
- [5] read `internal/ledger/store.go`
- [6] edited `internal/ledger/store.go` (+1 -1)
- [7] ran `go version` -> exit 127
- [8] finished: "changed 1 file(s): internal/ledger/store.go"

## Where it went wrong

- never ran a test that passed before finishing (no brokers in the sandbox, so the invariants could only be checked by the verifier)
- verifier: concurrent: 297 sagas with disallowed effects, 0 applied twice, 0 with wrong money (of 600) -> sagas left ledger operations their final state does not allow
- verifier: concurrent: at rest but 75933 held -> money is still held or in clearing after every saga finished
- verifier: concurrent: saga 01a19645-58f6-76a4-84c9-93772e178712 (race): ledger effects map[hold:ok release:ok/tombstone] not allowed for CANCELLED -> a saga left ledger operations its final state does not allow
- verifier: concurrent: saga 01a19645-58f6-76a7-bf93-d696184ecc9c (race): ledger effects map[hold:ok release:ok/tombstone] not allowed for CANCELLED -> a saga left ledger operations its final state does not allow
- verifier: concurrent: saga 01a19645-58f6-76a9-bdab-f1947f8804df (race): ledger effects map[hold:ok release:ok/tombstone] not allowed for CANCELLED -> a saga left ledger operations its final state does not allow
- verifier: concurrent: saga 01a19645-58f6-76ae-8cdc-6e669dffcc44 (race): ledger effects map[hold:ok release:ok/tombstone] not allowed for CANCELLED -> a saga left ledger operations its final state does not allow
- final message reports a finished change; the verifier disagrees

## Did it get around the verifier?

- no: no protected file changed, no network, the verifier ran on pristine harness files
