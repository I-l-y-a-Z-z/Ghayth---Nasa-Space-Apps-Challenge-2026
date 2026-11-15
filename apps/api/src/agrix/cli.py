"""Local setup, idempotent seeding and a complete HTTP-contract smoke flow."""

import argparse
import csv
import hashlib
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from agrix.catalog import load_catalog
from agrix.config import Settings
from agrix.db import Repository, create_schema, make_engine
from agrix.demo import demo_plot
from agrix.geometry import normalize_geometry
from agrix.models import Plot


def prepare_pilot(
    settings: Settings, project_id: str = "demo", wait_seconds: float = 60, *, pipeline_factory=None
) -> dict:
    """Explicitly create/reuse an illustrative live plot and confirmation-needed draft.

    The boundary is a testing example, not a verified farmer's parcel. No farm
    costs, wages, yield, prices, crop history or machinery capacity are seeded.
    """
    from agrix.jobs import AnalysisRunner
    from agrix.models import (
        Analysis,
        AnalysisRequest,
        CropSelection,
        EvaluationRequest,
        FarmConstraints,
        Plan,
        PlanCreate,
        PlotCreate,
    )
    from agrix.service import Service

    if not project_id or len(project_id) > 128:
        raise ValueError("Supply a project ID between 1 and 128 characters")
    if not 0 <= wait_seconds <= 120:
        raise ValueError("Wait must be between 0 and 120 seconds")
    marker_id = "live-pilot-" + hashlib.sha256(project_id.encode()).hexdigest()[:24]
    engine = make_engine(settings.database_url)
    runner = AnalysisRunner(engine, settings, pipeline_factory=pipeline_factory)
    catalog = load_catalog()
    try:
        with Session(engine) as session:
            with session.begin():
                repo = Repository(session)
                service = Service(
                    repo, catalog, project_id, runner=runner, default_analysis_mode="live"
                )
                marker = repo.get("local_records", marker_id) or {}
                if marker.get("plot_id") and repo.get("plots", marker["plot_id"]):
                    plot = service.get_plot(marker["plot_id"])
                else:
                    request = demo_plot()
                    plot = service.create_plot(
                        PlotCreate(
                            name="Settat live pilot — illustrative boundary, confirm farm inputs",
                            geometry=request.geometry,
                            analysis_mode="live",
                            constraints=FarmConstraints(
                                production_system="unknown",
                                setup_cost_mad=None,
                                wage_mad_per_person_day=None,
                            ),
                        )
                    )
                    marker = {
                        "id": marker_id,
                        "project_id": project_id,
                        "record_kind": "live_pilot_setup",
                        "plot_id": plot.id,
                        "synthetic": False,
                        "boundary_status": "illustrative_unverified",
                        "farm_inputs_status": "unknown",
                    }
                    repo.put("local_records", marker_id, marker)
                analysis = service.analyze(
                    plot.id,
                    plot.revision,
                    AnalysisRequest(start_date=date(2023, 11, 1), end_date=date(2025, 10, 31)),
                )
            # Provider workers start only after the plot/job transaction commits.
            runner.launch_committed(session)
        deadline = time.monotonic() + wait_seconds
        while True:
            with Session(engine) as session:
                current = Analysis.model_validate(Repository(session).get("analyses", analysis.id))
            if current.status not in {"pending", "running"} or time.monotonic() >= deadline:
                break
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        result = {
            "mode": "live",
            "project_id": project_id,
            "plot_id": plot.id,
            "analysis_id": current.id,
            "analysis_status": current.status,
            "boundary_status": "illustrative_unverified",
            "farm_inputs_status": "unknown",
            "period": current.period.model_dump(mode="json") if current.period else None,
        }
        if current.status in {"pending", "running", "failed"}:
            return {
                **result,
                "plan_id": None,
                "next_action": "Poll or retry the analysis; no evaluation was invented",
            }
        with Session(engine) as session, session.begin():
            repo = Repository(session)
            service = Service(
                repo, catalog, project_id, runner=runner, default_analysis_mode="live"
            )
            previous = repo.get("plans", marker["plan_id"]) if marker.get("plan_id") else None
            if (
                previous
                and previous.get("plot_revision") == plot.revision
                and (previous.get("evaluation_snapshot", {}) or {}).get("analysis_id") == current.id
            ):
                plan = Plan.model_validate(previous)
            else:
                evaluation = service.evaluate(
                    EvaluationRequest(
                        plot_id=plot.id,
                        plot_revision=plot.revision,
                        start_date=date(2025, 11, 1),
                        horizon_days=365,
                        sequence=[CropSelection(crop_id="wheat", start_date=date(2025, 11, 1))],
                    )
                )
                plan = service.create_plan(
                    PlanCreate(
                        plot_id=plot.id,
                        evaluation_id=evaluation.id,
                        status="draft",
                        name="Illustrative wheat draft — confirm farm inputs and dates",
                    )
                )
                marker = {**marker, "plan_id": plan.id}
                repo.put("local_records", marker_id, marker)
        return {
            **result,
            "plan_id": plan.id,
            "plan_status": plan.status,
            "evaluation_id": plan.evaluation_id,
            "evaluation_status": plan.evaluation_snapshot.status
            if plan.evaluation_snapshot
            else None,
            "daily_weather_rows": len(current.profile.weather) if current.profile else 0,
        }
    finally:
        runner.close()
        engine.dispose()


def seed(database_url: str) -> dict:
    from agrix.demo_seed import seed_workspace

    engine = make_engine(database_url)
    catalog = load_catalog()
    plot_ids = []
    try:
        with Session(engine) as session, session.begin():
            repo = Repository(session)
            repo.put(
                "local_records",
                "fixture-catalog-v1",
                {
                    "id": "fixture-catalog-v1",
                    "synthetic": True,
                    "version": catalog.version,
                    "catalog": catalog.model_dump(mode="json"),
                },
            )
            repo.put(
                "local_records",
                "fixture-service-v1",
                {
                    "id": "fixture-service-v1",
                    "synthetic": True,
                    "name": "Demo Farm Services — fictional",
                    "contact": None,
                    "verification_date": "2024-01-01",
                    "service": "machinery",
                },
            )
            for zone in ("settat", "meknes", "ouarzazate"):
                id = f"fixture-plot-{zone}"
                if repo.get("plots", id) is None:
                    request = demo_plot(zone)
                    geometry, area = normalize_geometry(request.geometry)
                    timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
                    plot = Plot(
                        id=id,
                        project_id="demo",
                        name=request.name,
                        revision=1,
                        analysis_mode="demo",
                        geometry=geometry,
                        area_ha=area,
                        constraints=request.constraints,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    repo.save_plot(plot.model_dump(mode="json"))
                plot_ids.append(id)
            workspace_seed = seed_workspace(repo, catalog)
    finally:
        engine.dispose()
    return {
        "mode": "synthetic_planning_with_sourced_weather_context",
        "catalog_version": catalog.version,
        "plot_ids": plot_ids,
        "workspace": workspace_seed,
    }


def smoke(client) -> dict:
    """Works with TestClient or a running local API through httpx.Client."""

    def call(method: str, path: str, payload: dict | None = None):
        response = client.request(
            method, path, **({"json": payload} if payload is not None else {})
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {response.status_code}: {response.text}")
        return response.json()

    plot = call("POST", "/plots", demo_plot().model_dump(mode="json"))
    analysis = call("POST", f"/plots/{plot['id']}/analyses", {"plot_revision": 1})
    repeated = call("POST", f"/plots/{plot['id']}/analyses", {"plot_revision": 1})
    assert analysis["id"] == repeated["id"], "Analysis cache did not reuse matching input"
    request = {
        "plot_id": plot["id"],
        "plot_revision": 1,
        "start_date": "2025-11-01",
        "horizon_days": 730,
    }
    suggested = call("POST", "/rotations/suggest", request)["candidates"][0]
    custom = call("POST", "/rotations/evaluate", suggested["input_snapshot"]["request"])
    assert custom["fingerprint"] == suggested["fingerprint"], "Suggestion/custom inputs diverged"
    assert custom["financials"] == suggested["financials"], "Suggestion/custom figures diverged"
    detail = call("GET", f"/evaluations/{custom['id']}")
    plan = call(
        "POST",
        "/plans",
        {
            "plot_id": plot["id"],
            "evaluation_id": detail["id"],
            "status": "selected",
            "name": "Synthetic smoke plan",
        },
    )
    reopened = call("GET", f"/plans/{plan['id']}")
    assert reopened["evaluation_snapshot"] == detail, "Saved evaluation changed"
    for locale in ("ary", "fr", "en"):
        reply = call(
            "POST",
            "/chat",
            {
                "plot_id": plot["id"],
                "evaluation_ids": [detail["id"]],
                "message": "Compare funding and margin",
                "locale": locale,
            },
        )
        assert reply["read_only"] and reply["synthetic"]
        assert (
            reply["facts"][0]["peak_funding_need_mad"]
            == (detail["financials"]["whole_plot"]["peak_funding_need"]["value"])
        )
    assert call("GET", f"/plans/{plan['id']}") == reopened, "Chat mutated a saved plan"
    return {
        "mode": "synthetic",
        "plot_id": plot["id"],
        "analysis_id": analysis["id"],
        "evaluation_id": detail["id"],
        "plan_id": plan["id"],
        "status": detail["status"],
        "duration_days": detail["rotation"]["duration_days"],
        "peak_funding_need_mad": detail["financials"]["whole_plot"]["peak_funding_need"]["value"],
        "checks": "analysis reuse, suggestion/custom parity, detail, save/reopen, three chat locales",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "init-db", help="Explicit development/test schema bootstrap; deployment uses Alembic"
    )
    sub.add_parser("seed", help="Idempotently seed synthetic plots and local records")
    sub.add_parser(
        "earthdata-login",
        help="Interactive NASA login on this terminal; persist through earthaccess, never print credentials",
    )
    for command in ("data-plan", "fetch-data"):
        data_parser = sub.add_parser(
            command, help="Plan or retrieve general NASA/SoilGrids subsets for any polygon"
        )
        data_parser.add_argument("--geojson", type=Path)
        data_parser.add_argument(
            "--bbox", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH")
        )
        data_parser.add_argument("--start")
        data_parser.add_argument("--end")
        data_parser.add_argument("--datasets", default="smap,soil,terrain,hls")
        data_parser.add_argument("--max-scenes", type=int, default=10)
    smoke_parser = sub.add_parser("smoke", help="Exercise the complete fixture API flow")
    smoke_parser.add_argument("--base-url", help="Use an already running local API")
    schema = sub.add_parser("openapi", help="Export the OpenAPI contract")
    schema.add_argument("--output", default="docs/openapi.json")
    asset_parser = sub.add_parser(
        "import-asset", help="Register a selected scientific file in the managed cache"
    )
    asset_parser.add_argument("path", type=Path)
    asset_parser.add_argument("--dataset-id", required=True)
    asset_parser.add_argument(
        "--format",
        required=True,
        choices=("power_json", "giovanni_csv", "geotiff", "netcdf", "parquet"),
    )
    asset_parser.add_argument("--variable")
    asset_parser.add_argument("--unit")
    asset_parser.add_argument("--version", default="unspecified")
    asset_parser.add_argument(
        "--metadata-json",
        type=Path,
        help="JSON object with bounds, depth, dates, quality and source metadata",
    )
    sub.add_parser(
        "provider-health",
        help="Show provider configuration, units and local asset integrity; no network request",
    )
    records_parser = sub.add_parser(
        "import-records",
        help="Validate and transactionally import sourced local CSV/JSON market/service records",
    )
    records_parser.add_argument("file", type=Path)
    records_parser.add_argument("--project-id", default="demo")
    pilot_parser = sub.add_parser(
        "prepare-pilot",
        help="Explicitly prepare an illustrative Settat live plot, actual profile and confirmation-needed draft",
    )
    pilot_parser.add_argument("--project-id", default="demo")
    pilot_parser.add_argument("--wait-seconds", type=float, default=60)
    args = parser.parse_args()
    settings = Settings.from_env()
    database_url = settings.database_url
    if args.command == "earthdata-login":
        import earthaccess

        auth = earthaccess.login(strategy="interactive", persist=True)
        result = {"authenticated": bool(auth.authenticated), "credentials_printed": False}
    elif args.command in {"data-plan", "fetch-data"}:
        from shapely.geometry import box

        from agrix.earth.acquisition import DataAcquirer, acquisition_plan, five_year_period

        first, last = five_year_period()
        start = date.fromisoformat(args.start) if args.start else first
        end = date.fromisoformat(args.end) if args.end else last
        if not start <= end <= date.today():
            parser.error("Choose an ordered historical period ending no later than today")
        if args.geojson:
            raw = json.loads(args.geojson.read_text())
            geometry = raw.get("geometry", raw)
        elif args.bbox:
            west, south, east, north = args.bbox
            if not -180 <= west < east <= 180 or not -90 <= south < north <= 90:
                parser.error("Invalid geographic bounding box")
            geometry = box(west, south, east, north).__geo_interface__
        else:
            parser.error("Supply --geojson FILE or --bbox WEST SOUTH EAST NORTH")
        datasets = args.datasets.split(",")
        if set(datasets) - {"smap", "soil", "terrain", "hls"} or not 1 <= args.max_scenes <= 120:
            parser.error("Datasets: smap,soil,terrain,hls; max-scenes must be 1..120")
        result = (
            acquisition_plan(
                box(*args.bbox).bounds
                if args.bbox
                else __import__("shapely").geometry.shape(geometry).bounds,
                start,
                end,
            )
            if args.command == "data-plan"
            else DataAcquirer(
                settings.cache_directory, network_enabled=settings.network_enabled
            ).acquire(geometry, start, end, datasets, args.max_scenes)
        )
    elif args.command == "prepare-pilot":
        result = prepare_pilot(settings, args.project_id, args.wait_seconds)
    elif args.command == "provider-health":
        from agrix.earth import ProfilePipeline

        result = ProfilePipeline(settings.cache_directory, settings.network_enabled).health()
    elif args.command == "import-asset":
        from agrix.earth import register_asset
        from agrix.earth.records import ProviderError
        from agrix.earth.weather import load_giovanni_csv, load_power_json

        try:
            metadata = (
                json.loads(args.metadata_json.read_text(encoding="utf-8"))
                if args.metadata_json
                else {}
            )
            if not isinstance(metadata, dict):
                raise ValueError("Metadata must be a JSON object")
            reserved = {
                "cache_dir",
                "path",
                "dataset_id",
                "format",
                "variable",
                "unit",
                "version",
            } & set(metadata)
            if reserved:
                raise ValueError(
                    "Set reserved metadata through CLI arguments: " + ", ".join(sorted(reserved))
                )
            if args.format in {"geotiff", "netcdf", "parquet"} and not args.variable:
                raise ValueError("Raster and Parquet inputs require --variable")
            if args.format in {"geotiff", "netcdf"} and not args.unit:
                raise ValueError("Scientific raster inputs require --unit")
            if args.format in {"power_json", "giovanni_csv"}:
                source = (
                    load_power_json(args.path)
                    if args.format == "power_json"
                    else load_giovanni_csv(args.path)
                )
                if args.dataset_id != source.dataset_id:
                    raise ValueError(
                        "--dataset-id contradicts the source file identity: " + source.dataset_id
                    )
                if args.version == "unspecified":
                    args.version = source.version
            result = register_asset(
                settings.cache_directory,
                args.path,
                args.dataset_id,
                args.format,
                variable=args.variable,
                unit=args.unit,
                version=args.version,
                **metadata,
            )
        except (OSError, ValueError, ProviderError) as exc:
            parser.error(str(exc))
    elif args.command == "import-records":
        from agrix.errors import DomainError
        from agrix.records import import_records

        try:
            if args.file.stat().st_size > 5 * 1024 * 1024:
                raise ValueError("Local record import is limited to 5 MiB")
            with args.file.open(encoding="utf-8-sig", newline="") as stream:
                if args.file.suffix.lower() == ".csv":
                    records = list(csv.DictReader(stream))
                else:
                    payload = json.load(stream)
                    records = payload.get("records") if isinstance(payload, dict) else payload
            engine = make_engine(database_url)
            try:
                with Session(engine) as session, session.begin():
                    result = import_records(Repository(session), records, args.project_id)
            finally:
                engine.dispose()
        except (OSError, ValueError, DomainError) as exc:
            parser.error(str(exc))
    elif args.command == "init-db":
        engine = make_engine(database_url)
        try:
            create_schema(engine)
        finally:
            engine.dispose()
        result = {"schema": "ready", "mode": "synthetic"}
    elif args.command == "seed":
        result = seed(database_url)
    elif args.command == "openapi":
        from agrix.api import create_app

        app = create_app(database_url)
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        app.state.engine.dispose()
        result = {"openapi": str(destination)}
    elif args.base_url:
        import httpx

        with httpx.Client(
            base_url=args.base_url, timeout=30, headers={"X-Project-ID": "demo"}
        ) as client:
            result = smoke(client)
    else:
        from fastapi.testclient import TestClient

        from agrix.api import create_app

        app = create_app(database_url)
        try:
            with TestClient(app) as client:
                result = smoke(client)
        finally:
            app.state.engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
