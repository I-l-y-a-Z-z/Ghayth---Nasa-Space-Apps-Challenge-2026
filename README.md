# Ghayth

Farm planning for Morocco in Darija, French and English. Draw a field, answer farming questions, compare crop rotations, then save or print a plan. The AgriX engine prioritizes environmental suitability and planting dates; historical Moroccan references supply supporting cost and sales estimates.

**[Open the public demo](https://ghayth-demo.pages.dev/app/)** — currently running prepared synthetic examples with browser-local saves. See [deployment details and limitations](docs/live-demo.md).

## Run locally

Use Python 3.10–3.12, Node 24 and npm 10+. Run commands from this repository root.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e './apps/api[dev,earthdata]'
npm ci
npm run build
cp .env.example .env
./scripts/start.sh
```

Open the address printed by the launcher, normally http://127.0.0.1:8000. It serves the client and API together. SQLite starts with an empty workspace; set `DATABASE_URL` to reuse PostgreSQL/PostGIS. The launcher reads `.env`. Keep an existing `.env` when upgrading.

New plots trigger background acquisition. First requests can take several minutes; later requests reuse the scientific cache. Missing layers remain visible and can be retried. Set `AGRIX_PROVIDER_NETWORK=false` and `AGRIX_REMOTE_ASSETS=0` for local-file/cache-only operation.

For frontend development, run `npm run dev` alongside the API on port 8000. API documentation is at `/docs`; health endpoints are `/health` and `/health/ready`.

## Repository

| Path | Purpose |
| --- | --- |
| `apps/web/` | React, TypeScript, Vite and MapLibre client |
| `apps/api/` | FastAPI engine, migrations and backend tests |
| `packages/ui/` | Shared React primitives and styles |
| `services/speech/` | Darija speech models and evaluation tools |
| `app.py`, `speech_api.py` | Product entry point and speech transport |
| `data/` | Source registry, samples and ignored runtime caches/models |
| `scripts/` | Start, build, verification and data preparation |
| `infrastructure/` | Browser security policies and tests |
| `tests/` | Product integration tests |
| `docs/` | Product, architecture, data and testing documentation |

Internal `agrix` names and `AGRIX_*` settings remain for compatibility. Ghayth is the product name. There is one application source tree.

## Verify

```bash
./scripts/check.sh
```

This checks TypeScript, browser security policies, the production build, backend behavior and speech integration without downloading model weights. See [testing](docs/testing.md) for optional browser/PostGIS checks. [apps/api/requirements.lock](apps/api/requirements.lock) pins the previously verified scientific/backend environment.

## Documentation

- [Product scope](docs/product-spec.md) and [architecture](docs/architecture.md)
- [Full rotation algorithm](docs/rotation-algorithm.md): search, equations, constraints, uncertainty and ranking
- [Model sources](docs/model-data-sources.md), [NASA setup](docs/data-setup.md), [Earth integration](docs/earth-integration.md) and [agronomic rules](docs/agronomy.md)
- [Automatic economics](docs/economics.md) and [local records/access](docs/local-records.md)
- [Optional speech setup](services/speech/README.md) and [source provenance](docs/provenance.md)
- [Free Render demo](docs/render.md): typed chat and planning with Neon/PostGIS, free instance limits and deployment
- [Cloudflare deployment](docs/cloudflare.md): optional Workers Paid container hosting with durable R2 scientific files

Docker/PostGIS configuration is in `compose.yaml`: `docker compose up --build`. The base image provides typed chat and planning; voice requires optional dependencies. Container execution has not been verified in this workspace.

Environmental outputs are historical screening estimates requiring local calibration. Economic estimates use historical references rather than current buyer or contractor quotes. Saved evaluations retain their inputs and evidence. Runtime credentials, databases, caches, audio and model weights stay outside source control.
