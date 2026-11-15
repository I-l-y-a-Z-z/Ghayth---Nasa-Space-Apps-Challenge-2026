"""Farm-planning API with live Earth profiles and explicitly labeled demo data."""

import asyncio
import os
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from importlib.resources import files
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from agrix.catalog import load_catalog
from agrix.config import Settings
from agrix.db import Repository, create_schema, make_engine
from agrix.errors import DomainError
from agrix.jobs import AnalysisRunner
from agrix.models import (
    Analysis,
    AnalysisRequest,
    Catalog,
    ChatReply,
    ChatRequest,
    ErrorResponse,
    Evaluation,
    EvaluationRequest,
    Plan,
    PlanCreate,
    PlanPatch,
    Plot,
    PlotCreate,
    PlotPatch,
    SuggestionRequest,
    Suggestions,
    Workspace,
)
from agrix.service import Service


def service_dependency(
    request: Request,
    project_id: Annotated[str, Header(alias="X-Project-ID", min_length=1, max_length=128)] = "demo",
) -> Iterator[Service]:
    with Session(request.app.state.engine) as session:
        try:
            yield Service(
                Repository(session),
                request.app.state.catalog,
                project_id,
                runner=request.app.state.runner,
                default_analysis_mode=request.app.state.settings.default_analysis_mode,
            )
            session.commit()
            request.app.state.runner.launch_committed(session)
        except Exception:
            session.rollback()
            raise


ServiceDependency = Annotated[Service, Depends(service_dependency, scope="function")]


def create_app(
    database_url: str | None = None,
    *,
    initialize: bool = False,
    default_analysis_mode: str | None = None,
    settings: Settings | None = None,
    pipeline_factory=None,
    cache_store=None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings = replace(
        settings,
        database_url=database_url or settings.database_url,
        default_analysis_mode=default_analysis_mode or settings.default_analysis_mode,
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            if app.state.cache_store is not None:
                await asyncio.to_thread(app.state.cache_store.restore)
            app.state.runner.resume_pending()
            yield
        finally:
            app.state.runner.close()
            app.state.engine.dispose()

    app = FastAPI(
        title="AgriX — Morocco farm planning",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "Map-based farm planning with asynchronous Earth-observation profiles, qualified Moroccan "
            "crop references, historical climate replay, cash-flow estimates and localized chat. "
            "Live and demo plots are separate. Missing local evidence remains explicit; no calibrated "
            "yield forecast is claimed. Configure workspace authentication before deployment."
        ),
    )
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)
    if cache_store is None:
        from agrix.earth.object_store import cache_store_from_env

        cache_store = cache_store_from_env(settings.cache_directory)
    app.state.cache_store = cache_store
    app.state.runner = AnalysisRunner(
        app.state.engine, settings, pipeline_factory=pipeline_factory, cache_store=cache_store
    )
    app.state.catalog = load_catalog()
    if initialize:
        create_schema(app.state.engine)
    static_directory = str(files("agrix").joinpath("static"))
    app.mount("/assets", StaticFiles(directory=static_directory, check_dir=False), name="assets")
    app_directory = str(files("agrix").joinpath("static/appbuild"))
    app.mount("/app", StaticFiles(directory=app_directory, check_dir=False), name="application")

    @app.get("/", include_in_schema=False)
    def farmer_ui():
        from pathlib import Path

        path = Path(app_directory) / "index.html"
        return FileResponse(path if path.exists() else f"{static_directory}/index.html")

    @app.get("/testing", include_in_schema=False)
    def testing_ui():
        return FileResponse(f"{static_directory}/index.html")

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Do not echo non-JSON NaN/Infinity values (or arbitrary input/exception objects).
        details = [
            {"loc": list(e["loc"]), "type": e["type"], "message": e["msg"]} for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Request validation failed",
                    "details": details,
                }
            },
        )

    @app.exception_handler(IntegrityError)
    async def state_conflict(request: Request, exc: IntegrityError):
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "STATE_CONFLICT",
                    "message": "Inputs changed concurrently; read current state and retry",
                }
            },
        )

    @app.exception_handler(OperationalError)
    async def database_unavailable(request: Request, exc: OperationalError):
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "Database unavailable; check configuration and migrations",
                }
            },
        )

    errors = {
        404: {"model": ErrorResponse, "description": "Unknown ID or unavailable project scope"},
        409: {"model": ErrorResponse, "description": "Stale revision or concurrent input change"},
        422: {
            "model": ErrorResponse,
            "description": "Invalid geometry, dates, constraints, or state",
        },
        503: {"model": ErrorResponse, "description": "Configured database unavailable"},
    }

    @app.get("/health", tags=["system"])
    def health():
        return {
            "status": "ok",
            "mode": settings.default_analysis_mode,
            "version": "1.0.0",
        }

    @app.get("/health/ready", tags=["system"])
    def ready():
        with app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1 FROM plots LIMIT 1"))
        return {
            "status": "ready",
            "database": app.state.engine.dialect.name,
            "default_analysis_mode": settings.default_analysis_mode,
        }

    @app.get("/_internal/runtime", include_in_schema=False)
    def runtime(request: Request):
        token = os.getenv("AGRIX_INTERNAL_TOKEN", "")
        supplied = request.headers.get("X-Agrix-Internal-Token", "")
        if not token or not compare_digest(token.encode(), supplied.encode()):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return {
            "busy": app.state.runner.has_work() or bool(getattr(app.state, "speech_tasks", set()))
        }

    @app.get("/client-config", tags=["system"])
    def client_config():
        return {
            "map_style_url": settings.map_style_url,
            "map_tile_url": settings.map_tile_url,
            "geocoder_url": settings.geocoder_url,
            "authentication_required": bool(settings.access_password),
            "default_analysis_mode": settings.default_analysis_mode,
        }

    @app.get("/workspace/ingestion", tags=["system"], responses=errors)
    def ingestion(service: ServiceDependency):
        from datetime import datetime, timezone

        from agrix.earth import ProfilePipeline

        rows = service.repo.list_project("analyses", service.project_id)
        jobs = [
            {
                key: row.get(key)
                for key in (
                    "id",
                    "plot_id",
                    "plot_revision",
                    "status",
                    "created_at",
                    "completed_at",
                    "period",
                    "error",
                    "synthetic",
                )
            }
            for row in rows
        ]
        jobs.sort(key=lambda row: row["created_at"], reverse=True)
        return {
            "checked_at": datetime.now(timezone.utc),
            "providers": ProfilePipeline(
                settings.cache_directory, settings.network_enabled
            ).health(),
            "jobs": jobs[:100],
            "counts": {
                status: sum(row["status"] == status for row in jobs)
                for status in ("pending", "running", "complete", "partial", "unsupported", "failed")
            },
        }

    @app.get("/catalog", response_model=Catalog, tags=["catalog"])
    def catalog(mode: str = "demo"):
        if mode == "live":
            from agrix.agronomy import load_agronomic_catalog

            return load_agronomic_catalog()
        return app.state.catalog

    @app.get("/workspace", response_model=Workspace, responses=errors, tags=["testing"])
    def workspace(service: ServiceDependency):
        return service.workspace()

    @app.post("/demo/seed", response_model=Workspace, responses=errors, tags=["testing"])
    def seed_demo(service: ServiceDependency):
        from agrix.demo_seed import seed_workspace

        seed_workspace(service.repo, service.catalog, service.project_id)
        return service.workspace()

    @app.post("/plots", response_model=Plot, status_code=201, responses=errors, tags=["plots"])
    def create_plot(body: PlotCreate, service: ServiceDependency):
        return service.create_plot(body)

    @app.get("/plots/{plot_id}", response_model=Plot, responses=errors, tags=["plots"])
    def get_plot(plot_id: str, service: ServiceDependency):
        return service.get_plot(plot_id)

    @app.patch("/plots/{plot_id}", response_model=Plot, responses=errors, tags=["plots"])
    def patch_plot(plot_id: str, body: PlotPatch, service: ServiceDependency):
        return service.patch_plot(plot_id, body)

    @app.get(
        "/plots/{plot_id}/analyses/latest",
        response_model=Analysis,
        responses=errors,
        tags=["analyses"],
    )
    def latest_analysis(plot_id: str, service: ServiceDependency):
        return service.latest_analysis(plot_id)

    @app.post(
        "/plots/{plot_id}/analyses", response_model=Analysis, responses=errors, tags=["analyses"]
    )
    def analyze(plot_id: str, service: ServiceDependency, body: AnalysisRequest | None = None):
        return service.analyze(plot_id, body.plot_revision if body else None, body)

    @app.post(
        "/analyses/{analysis_id}/retry",
        response_model=Analysis,
        responses=errors,
        tags=["analyses"],
    )
    def retry_analysis(
        analysis_id: str, service: ServiceDependency, body: AnalysisRequest | None = None
    ):
        return service.retry_analysis(analysis_id, body)

    @app.get(
        "/analyses/{analysis_id}", response_model=Analysis, responses=errors, tags=["analyses"]
    )
    def get_analysis(analysis_id: str, service: ServiceDependency):
        return service.get("analyses", analysis_id, Analysis)

    @app.post(
        "/rotations/suggest", response_model=Suggestions, responses=errors, tags=["rotations"]
    )
    def suggest(body: SuggestionRequest, service: ServiceDependency):
        return service.suggest(body)

    @app.post(
        "/rotations/evaluate", response_model=Evaluation, responses=errors, tags=["rotations"]
    )
    def evaluate_rotation(body: EvaluationRequest, service: ServiceDependency):
        return service.evaluate(body)

    @app.get(
        "/evaluations/{evaluation_id}",
        response_model=Evaluation,
        responses=errors,
        tags=["rotations"],
    )
    def get_evaluation(evaluation_id: str, service: ServiceDependency):
        return service.get("evaluations", evaluation_id, Evaluation)

    @app.post("/plans", response_model=Plan, status_code=201, responses=errors, tags=["plans"])
    def create_plan(body: PlanCreate, service: ServiceDependency):
        return service.create_plan(body)

    @app.get("/plans/{plan_id}", response_model=Plan, responses=errors, tags=["plans"])
    def get_plan(plan_id: str, service: ServiceDependency):
        return service.get("plans", plan_id, Plan)

    @app.patch("/plans/{plan_id}", response_model=Plan, responses=errors, tags=["plans"])
    def patch_plan(plan_id: str, body: PlanPatch, service: ServiceDependency):
        return service.patch_plan(plan_id, body)

    @app.post("/chat", response_model=ChatReply, responses=errors, tags=["chat"])
    def chat(body: ChatRequest, service: ServiceDependency):
        return service.chat(body)

    @app.post("/records/import", responses=errors, tags=["local records"])
    def import_records(body: list[dict], service: ServiceDependency):
        from agrix.records import import_records

        if len(body) > 1000:
            raise DomainError("TOO_MANY_RECORDS", "Import at most 1000 records per request")
        return import_records(service.repo, body, service.project_id)

    @app.get("/market-history", responses=errors, tags=["local records"])
    def market_history(plot_id: str, service: ServiceDependency, crop_id: str | None = None):
        from agrix.records import get_market_history

        plot = service.get_plot(plot_id)
        return get_market_history(
            service.repo, plot, service.profile_for(plot).profile, [crop_id] if crop_id else []
        )

    @app.get("/services", responses=errors, tags=["local records"])
    def local_services(plot_id: str, service: ServiceDependency):
        from agrix.records import get_services

        plot = service.get_plot(plot_id)
        return get_services(service.repo, plot, service.profile_for(plot).profile)

    from agrix.access import install_access_control

    install_access_control(app, settings)

    return app


app = create_app()
