#!/usr/bin/env sh
# Use an explicit existing test database, or create one in the local Compose service.
set -eu

cd "$(dirname "$0")/.."

if [ -z "${AGRIX_TEST_POSTGIS_URL:-}" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' 'Docker is unavailable. Set AGRIX_TEST_POSTGIS_URL to an existing PostgreSQL *_test database.' >&2
    exit 1
  fi
  docker compose up -d --wait db
  if ! docker compose exec -T db psql -U agrix -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = 'agrix_test'" | tr -d '[:space:]' | grep -q '^1$'; then
    docker compose exec -T db createdb -U agrix agrix_test
  fi
  AGRIX_TEST_POSTGIS_URL='postgresql+psycopg://agrix:agrix@127.0.0.1:5432/agrix_test'
  export AGRIX_TEST_POSTGIS_URL
fi

# Check the target before applying migrations, without printing credentials.
python - <<'PY'
import os
from sqlalchemy.engine import make_url

url = make_url(os.environ["AGRIX_TEST_POSTGIS_URL"])
if url.get_backend_name() != "postgresql" or not (url.database or "").endswith("_test"):
    raise SystemExit("PostGIS verification requires a dedicated PostgreSQL database ending in _test")
PY

DATABASE_URL="$AGRIX_TEST_POSTGIS_URL"
export DATABASE_URL
python -m alembic upgrade head
python -m pytest -q -m postgis
