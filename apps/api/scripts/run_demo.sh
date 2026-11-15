#!/usr/bin/env bash
# Start the local testing UI. Keep this terminal open; Ctrl+C stops the API.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  printf '%s\n' 'Create .venv and install the project first; see README.md.' >&2
  exit 1
fi
app_index="src/agrix/static/appbuild/index.html"
frontend_build_needed=false
if [[ ! -f "$app_index" ]]; then
  frontend_build_needed=true
else
  while IFS= read -r frontend_source; do
    if [[ "$frontend_source" -nt "$app_index" ]]; then
      frontend_build_needed=true
      break
    fi
  done < <(find frontend/src frontend/scripts -type f)
  for frontend_source in frontend/package.json frontend/package-lock.json frontend/index.html frontend/vite.config.ts frontend/tsconfig.json; do
    if [[ "$frontend_source" -nt "$app_index" ]]; then
      frontend_build_needed=true
      break
    fi
  done
fi
if [[ "$frontend_build_needed" == true ]]; then
  if ! command -v npm >/dev/null 2>&1; then
    printf '%s\n' 'Install Node.js 22+ and npm to build the browser application; see README.md.' >&2
    exit 1
  fi
  if [[ ! -d frontend/node_modules || frontend/package-lock.json -nt frontend/node_modules/.package-lock.json ]]; then
    npm --prefix frontend ci
  fi
  npm --prefix frontend run build
fi
export DATABASE_URL="${DATABASE_URL:-sqlite+pysqlite:///./agrix-demo.sqlite3}"
if [[ "$DATABASE_URL" == sqlite* ]]; then
  .venv/bin/python -m agrix.cli init-db
else
  .venv/bin/python -m alembic upgrade head
fi
.venv/bin/python -m agrix.cli seed
printf '\nOpen http://127.0.0.1:%s in your browser.\n' "${PORT:-8000}"
exec .venv/bin/python -m uvicorn agrix.api:app --host 127.0.0.1 --port "${PORT:-8000}"
