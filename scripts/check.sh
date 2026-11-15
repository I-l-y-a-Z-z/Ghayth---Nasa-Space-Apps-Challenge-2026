#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

product_python="${GHAYTH_PYTHON:-$PWD/.venv/bin/python}"
if [[ ! -x "$product_python" ]]; then
  printf '%s\n' 'Install the backend development environment or set GHAYTH_PYTHON.' >&2
  exit 1
fi

npm run check
export PYTHONPATH="$PWD/apps/api/src:$PWD/services/speech:$PWD${PYTHONPATH:+:$PYTHONPATH}"
"$product_python" -m pytest apps/api/tests tests services/speech/tests "$@" -q
