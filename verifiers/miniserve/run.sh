#!/usr/bin/env bash
# mini-serve verifier. Usage: run.sh REPO OUT_JSON (needs `make deps`: library, venv, weights)
here=$(cd "$(dirname "$0")" && pwd)
exec "$here/../../.cache/venv-miniserve/bin/python" "$here/verify.py" "$1" "$2"
