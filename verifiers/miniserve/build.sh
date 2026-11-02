#!/usr/bin/env bash
# Builds libminiserve once from the pinned upstream commit into .cache/miniserve-build, and the
# verifier's Python env. The engine (src/, CMakeLists.txt) is a protected path, so every
# repository under test runs on this same library.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
commit=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "$root/tasks/miniserve-preempt-frees-shared-block/task.json")
src=$root/.cache/miniserve-src build=$root/.cache/miniserve-build
if [ ! -f "$build/libminiserve.dylib" ] || [ "$(cat "$build/commit" 2>/dev/null)" != "$commit" ]; then
  rm -rf "$src" && mkdir -p "$src"
  git -C "$root/upstream/mini-serve" archive "$commit" src CMakeLists.txt | tar -x -C "$src"
  cmake -S "$src" -B "$build" -DCMAKE_BUILD_TYPE=Release >/dev/null
  cmake --build "$build" -j >/dev/null
  echo "$commit" > "$build/commit"
fi
# The model weights, by the pinned commit's own script (HF cache, sha256-checked; skips files present).
git -C "$root/upstream/mini-serve" show "$commit:scripts/fetch_weights.sh" | bash >/dev/null
test -x "$root/.cache/venv-miniserve/bin/python" || uv venv -q -p 3.12 "$root/.cache/venv-miniserve"
uv pip install -q -p "$root/.cache/venv-miniserve" -r "$root/verifiers/miniserve/requirements.txt"
