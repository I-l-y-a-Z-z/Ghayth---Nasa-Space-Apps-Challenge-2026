# Verification

Run from the root after installing backend development dependencies and running `npm ci`:

```bash
./scripts/check.sh
```

The script checks TypeScript, browser security policies, the production build, backend behavior and speech integration/development tools. Provider tests use mocked HTTP and small raster/NetCDF files. Speech tests inject models and do not download weights or measure accuracy.

Individual checks:

```bash
npm run check
PYTHONPATH=apps/api/src:services/speech:. .venv/bin/python -m pytest apps/api/tests tests services/speech/tests -q
.venv/bin/python -m ruff check apps/api/src apps/api/tests apps/api/migrations
.venv/bin/python -m ruff format --check apps/api/src apps/api/tests apps/api/migrations
```

## Cleanup verification — 2026-11-15

From the consolidated root: TypeScript, both security test files and the production build passed. Python: **228 passed, 5 optional skips**, plus 9 passing subtests. Ruff lint and formatting passed for 59 backend files. The HTTP tests required execution outside this environment's sandbox because its async thread wakeups stalled inside the sandbox, including before the cleanup.

## Optional integration checks

Browser/PostGIS tests are skipped unless explicitly configured. Chrome/Chromium and the backend `browser` extra are needed for browser tests.

```bash
AGRIX_BROWSER_TEST_URL=http://127.0.0.1:8000 \
  .venv/bin/python -m pytest apps/api/tests -q -m browser

AGRIX_TEST_POSTGIS_URL='postgresql+psycopg://user:password@localhost/ghayth_test' \
  ./scripts/postgis_test.sh
```

PostGIS checks require a dedicated disposable database ending in `_test`. They apply migrations and create test records. The helper does not create a database automatically.

The CLI exposes `agrix smoke`, `agrix provider-health` and `agrix openapi --output /tmp/ghayth-openapi.json`. Smoke creates demo records; use a disposable workspace. OpenAPI is generated on demand and available at `/openapi.json` on a running server.

## Recorded evidence

[evidence/](evidence/) retains supplied NASA acquisition, live-engine and speech smoke records. They describe specific local runs/periods and do not establish current service availability, field accuracy or agricultural speech quality. Local calibration and native-listener evaluation remain separate. Docker execution remains unverified.

Manual checks: draw/confirm a plot, inspect partial layers, answer unknown/rainfed/irrigated questions, compare distinct suggestions, evaluate a custom rotation, save/reopen, ask an engine-grounded question, review/edit a voice transcript, and switch languages at phone width.
