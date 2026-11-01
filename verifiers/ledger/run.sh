#!/usr/bin/env bash
# ledger-saga verifier, two parts on the repository under test:
#   1. sabrace (bench-owned, copied in as cmd/sabrace): the real ledger.Store
#      handling the commands of one saga concurrently, hundreds of times;
#   2. the project's own crash harness (cmd/ledgersaga chaos, restored from
#      upstream by bench/workspace.py) on the given crash points.
# Usage: run.sh REPO OUT_JSON POINTS
set -uo pipefail
repo=$1 out=$2 points=$3
here=$(cd "$(dirname "$0")" && pwd)
pg=${SAB_PG_URL:-postgres://postgres:postgres@localhost:23432/postgres?sslmode=disable}
cd "$repo"
mkdir -p cmd/sabrace && cp "$here/sabrace.go" cmd/sabrace/main.go
# The harness hard-codes ports 21000+; take them from the environment so the
# bench can run next to the project's own `make chaos`.
sed -i.orig 's/base := 21000 + h.slot\*10/base := envInt("CHAOS_PORT_BASE", 21000) + h.slot*10/' cmd/ledgersaga/chaos.go
if ! { go build -o bin/ledgersaga ./cmd/ledgersaga && go build -o bin/sabrace ./cmd/sabrace; } > build.log 2>&1; then
  python3 "$here/summary.py" --build-failed build.log "$out"; exit 1
fi
# SAB_CHAOS_ONLY=1 skips the stress (scripts/race_detection.py measures the harness alone).
if [ "${SAB_CHAOS_ONLY:-}" = 1 ]; then echo '{"problems": [], "metrics": {"skipped": true}}' > race.json
else ./bin/sabrace -admin "$pg" -seed "${SAB_SEED:-1}" -out race.json > race.log 2>&1; fi
n=$(tr ',' '\n' <<<"$points" | wc -l | tr -d ' ')
CHAOS_PORT_BASE=${SAB_PORT_BASE:-23100} KAFKA_BROKERS=${SAB_KAFKA:-localhost:23092} PG_ADMIN_URL=$pg \
  ./bin/ledgersaga chaos -points "$points" -seeds "${SAB_LEDGER_SEEDS:-1}" -parallel "$n" \
    -out chaos.md -json chaos.json -logs raw > chaos.log 2>&1
python3 "$here/summary.py" race.json chaos.json "$out"
