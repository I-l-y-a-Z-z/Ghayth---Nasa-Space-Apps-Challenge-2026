# Architecture

Ghayth is one web application with a Python planning engine. `app.py` creates the FastAPI app from `agrix.api`, then installs speech routes through `speech_api.py`. The production React build is served by the same origin under `/app/`.

## Request flow

1. The client collects a polygon. The API validates it and calculates geodesic hectares.
2. A persisted background analysis builds an environmental profile from NASA and SoilGrids inputs. Progress and retries belong to the selected plot revision.
3. The farmer supplies irrigation, tractor access and crop history. Unknown answers are supported; economic references are resolved automatically.
4. The engine searches sequences and sowing dates, estimates duration, then assesses complete rotations. Suggestions and custom plans use the same evaluator.
5. The client displays up to three distinct suggestions and a plan calendar. Saving retains a versioned evaluation snapshot.
6. The assistant explains stored results or requests an explicit what-if evaluation. Reviewed voice transcripts use the same `/chat` endpoint.

## Backend modules

Paths below are relative to `apps/api/src/agrix/`.

| Module | Responsibility |
| --- | --- |
| `api.py`, `service.py` | HTTP contracts and orchestration |
| `models.py`, `geometry.py` | Domain objects, units and polygons |
| `db.py`, `access.py` | Persistence, revisions, project scoping and signed sessions |
| `jobs.py`, `earth/` | Background analysis, acquisition, extraction, cache and provenance |
| `agronomy/planning.py` | Candidate search, calendars and sequence checks |
| `agronomy/environment.py` | Thermal duration, evapotranspiration, water balance and ranking |
| `evaluator.py`, `finance.py` | Shared evaluation, operations and cash flow |
| `economics.py`, `records.py` | Moroccan references and applicable local overrides |
| `chat.py` | Grounded explanations and engine-backed what-if requests |
| `fixtures/` | Versioned catalogs, parameters, economics and labeled test/demo data |

PostgreSQL/PostGIS is the deployment database, with Alembic migrations in `apps/api/migrations/`. SQLite is an explicit local/test option. The background executor is process-local; use one API worker. Scientific assets live in `data/cache/`, independently of application records.

## Boundaries

The [Cloudflare deployment](cloudflare.md) runs the same application in one Linux container. Hosted PostgreSQL/PostGIS persists farm records; R2 stores checksum-verified scientific checkpoints. Cache restoration precedes job resumption, and terminal analyses wait for a successful checkpoint. Durable alarms keep background work active after a browser closes. The container may sleep when idle and restart on demand.

Missing measurements remain unknown. Evidence records carry source identity, dates, units, resolution and limitations. Coarse grids remain regional context. Evaluations freeze the plot revision, profile, catalog and assumptions; new downloads do not rewrite saved plans.

The engine runs without an LLM. Optional intent parsing cannot supply independent financial facts. Speech models provide recognition and synthesis; they do not decide crops or change plans. Standalone speech development tools are separate from mounted product routes.

The client supports Arabic-script Darija with RTL, French and English. Provider credentials and weights are server-side. The optional shared-password gate scopes workspaces; it is not a complete account/roles system.
