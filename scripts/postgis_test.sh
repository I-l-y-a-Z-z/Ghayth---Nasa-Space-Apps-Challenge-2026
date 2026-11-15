#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

product_python="${GHAYTH_PYTHON:-$PWD/.venv/bin/python}"
: "${AGRIX_TEST_POSTGIS_URL:?Set a dedicated PostgreSQL/PostGIS database URL ending in _test.}"
export PYTHONPATH="$PWD/apps/api/src:$PWD/services/speech:$PWD${PYTHONPATH:+:$PYTHONPATH}"

"$product_python" - <<'CHECK_DATABASE'
import os
from sqlalchemy.engine import make_url

url = make_url(os.environ["AGRIX_TEST_POSTGIS_URL"])
if url.get_backend_name() != "postgresql" or not (url.database or "").endswith("_test"):
    raise SystemExit("PostGIS verification requires a dedicated PostgreSQL database ending in _test")
CHECK_DATABASE

DATABASE_URL="$AGRIX_TEST_POSTGIS_URL" "$product_python" -m alembic -c apps/api/alembic.ini upgrade head
"$product_python" -m pytest apps/api/tests -q -m postgis
