#!/usr/bin/env bash
# pg-search-sync verifier: real cdc binary, kill -9 / restart under write
# load, topic reconciled against the table by sabcheck (bench-owned).
# Usage: run.sh REPO OUT_JSON
set -uo pipefail
repo=$1 out=$2
here=$(cd "$(dirname "$0")" && pwd)
cd "$repo"
mkdir -p cmd/sabcheck && cp "$here/sabcheck.go" cmd/sabcheck/main.go
# The schema comes from the pinned upstream commit, not from the repo under test.
init=$(mktemp) && git -C "$here/../../upstream/pg-search-sync" show "$SAB_COMMIT:deploy/postgres/init.sql" > "$init"
if ! { go build -o bin/cdc ./cmd/cdc && go build -o bin/sabcheck ./cmd/sabcheck; } > build.log 2>&1; then
  python3 -c 'import json,sys; json.dump({"pass": False, "stage": "build", "problems": ["build failed: " + open(sys.argv[1]).read()[-2000:]], "metrics": {}}, open(sys.argv[2], "w"))' build.log "$out"
  exit 1
fi
./bin/sabcheck -cdc bin/cdc -init "$init" -out "$out" -log cdc.log -seed "${SAB_SEED:-1}" \
  -brokers "${SAB_KAFKA:-localhost:23092}" \
  -admin "${SAB_PG_URL:-postgres://postgres:postgres@localhost:23432/postgres?sslmode=disable}" > sabcheck.log 2>&1
rc=$?; rm -f "$init"; exit $rc
