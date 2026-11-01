#!/usr/bin/env bash
# events-lakehouse verifier. Usage: run.sh REPO OUT_JSON
here=$(cd "$(dirname "$0")" && pwd)
exec "${SAB_LAKEHOUSE_PY:-$here/../../.cache/venv-lakehouse/bin/python}" "$here/verify.py" "$1" "$2"
