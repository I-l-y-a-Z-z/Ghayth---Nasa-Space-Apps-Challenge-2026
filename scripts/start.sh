#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then set -a; source .env; set +a; fi
export PYTHONPATH="$PWD/apps/api/src:$PWD/services/speech:$PWD${PYTHONPATH:+:$PYTHONPATH}"
export DATABASE_URL="${DATABASE_URL:-sqlite+pysqlite:///$PWD/data/ghayth.sqlite3}"
export AGRIX_DEFAULT_ANALYSIS_MODE="${AGRIX_DEFAULT_ANALYSIS_MODE:-live}"
export AGRIX_REMOTE_ASSETS="${AGRIX_REMOTE_ASSETS:-1}"
export AGRIX_HLS_MAX_SCENES="${AGRIX_HLS_MAX_SCENES:-10}"
export AGRIX_CACHE_DIRECTORY="${AGRIX_CACHE_DIRECTORY:-$PWD/data/cache}"
product_python="${GHAYTH_PYTHON:-$PWD/.venv/bin/python}"
if [[ ! -x "$product_python" ]]; then printf '%s\n' 'Run the setup commands in README.md or set GHAYTH_PYTHON.' >&2; exit 1; fi
if [[ ! -f apps/api/src/agrix/static/appbuild/index.html ]]; then npm ci; npm run build; fi
if [[ "$DATABASE_URL" != sqlite* ]]; then (cd apps/api; "$product_python" -m alembic upgrade head); fi
printf 'Ghayth: http://127.0.0.1:%s\n' "${PORT:-8000}"
exec "$product_python" -m uvicorn app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
