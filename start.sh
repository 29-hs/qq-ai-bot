#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
  exec .venv/bin/python run.py "$@"
fi

if [ -d ".deps" ]; then
  export PYTHONPATH="$PWD/.deps${PYTHONPATH:+:$PYTHONPATH}"
fi

exec python3 run.py "$@"
