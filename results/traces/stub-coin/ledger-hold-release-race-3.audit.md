# Trace audit: ledger-hold-release-race / stub-coin #3

**Verdict: NOT RESOLVED**

Sandbox: network `none`. Change: 1 file(s), +1 -1 lines. Verifier: fail in 41.6 s.

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
- verifier: concurrent: 294 sagas with disallowed effects, 0 applied twice, 0 with wrong money (of 600) -> sagas left ledger operations their final state does not allow
- verifier: concurrent: at rest but 76245 held -> money is still held or in clearing after every saga finished
- verifier: concurrent: saga 01a19653-f090-7432-880c-16a3b9641359 (race): ledger effects map[hold:ok release:ok/tombstone] not allowed for CANCELLED -> a saga left ledger operations its final state does not allow
- verifier: concurrent: saga 01a19653-f090-7436-9e25-1f02ef80ec42 (race): ledger effects map[hold:ok release:ok/tombstone] not allowed for CANCELLED -> a saga left ledger operations its final state does not allow
- verifier: concurrent: saga 01a19653-f090-743b-9c3f-98bf3132c15d (race): ledger effects map[hold:ok release:ok/tombstone] not allowed for CANCELLED -> a saga left ledger operations its final state does not allow
- verifier: concurrent: saga 01a19653-f090-7441-bffb-1f7784aba978 (race): ledger effects map[hold:ok release:ok/tombstone] not allowed for CANCELLED -> a saga left ledger operations its final state does not allow
- final message reports a finished change; the verifier disagrees

## Did it get around the verifier?

- no: no protected file changed, no network, the verifier ran on pristine harness files
