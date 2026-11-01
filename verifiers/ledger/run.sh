#!/usr/bin/env bash
# ledger-saga verifier: the project's own crash harness (cmd/ledgersaga chaos,
# restored from upstream by bench/workspace.py) on the given crash points.
# Usage: run.sh REPO OUT_JSON POINTS
set -uo pipefail
repo=$1 out=$2 points=$3
here=$(cd "$(dirname "$0")" && pwd)
cd "$repo"
# The harness hard-codes ports 21000+; take them from the environment so the
# bench can run next to the project's own `make chaos`.
sed -i.orig 's/base := 21000 + h.slot\*10/base := envInt("CHAOS_PORT_BASE", 21000) + h.slot*10/' cmd/ledgersaga/chaos.go
if ! go build -o bin/ledgersaga ./cmd/ledgersaga > build.log 2>&1; then
  python3 "$here/summary.py" --build-failed build.log "$out"; exit 1
fi
n=$(tr ',' '\n' <<<"$points" | wc -l | tr -d ' ')
CHAOS_PORT_BASE=${SAB_PORT_BASE:-31000} KAFKA_BROKERS=${SAB_KAFKA:-localhost:29192} \
PG_ADMIN_URL=${SAB_PG_URL:-postgres://postgres:postgres@localhost:25432/postgres?sslmode=disable} \
  ./bin/ledgersaga chaos -points "$points" -seeds "${SAB_LEDGER_SEEDS:-1}" -parallel "$n" \
    -out chaos.md -json chaos.json -logs raw > chaos.log 2>&1
python3 "$here/summary.py" chaos.json "$out"
