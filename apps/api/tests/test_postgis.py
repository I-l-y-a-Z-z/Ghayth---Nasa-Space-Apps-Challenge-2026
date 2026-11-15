"""Repository invariants and an opt-in roundtrip against real PostgreSQL/PostGIS."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agrix.db import (
    AnalysisRow,
    EvaluationRow,
    PlanRow,
    PlotRevisionRow,
    PlotRow,
    Repository,
    create_schema,
    make_engine,
)


def _postgis_test_url():
    url = os.getenv("AGRIX_TEST_POSTGIS_URL")
    if not url:
        pytest.skip("Set AGRIX_TEST_POSTGIS_URL to a dedicated *_test PostGIS database")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("AGRIX_TEST_POSTGIS_URL must use PostgreSQL, not a substitute database")
    if not (parsed.database or "").endswith("_test"):
        pytest.fail("Integration tests require a dedicated database name ending in _test")
    return url


def test_sqlite_repository_preserves_revisions_and_rejects_cross_plot_plan():
    engine = make_engine("sqlite://")
    create_schema(engine)
    plot = {
        "id": "plot-a",
        "project_id": "project-a",
        "revision": 1,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-7.6, 33.0], [-7.5, 33.0], [-7.5, 33.1], [-7.6, 33.0]]],
        },
    }
    analysis = {
        "id": "analysis-a",
        "plot_id": "plot-a",
        "plot_revision": 1,
        "fixture_version": "fixture-v1",
    }
    evaluation = {
        "id": "evaluation-a",
        "plot_id": "plot-a",
        "plot_revision": 1,
        "analysis_id": "analysis-a",
        "margin_mad": "123.45",
    }
    with Session(engine) as session, session.begin():
        repo = Repository(session)
        repo.save_plot(plot)
        repo.put("analyses", analysis["id"], analysis)
        repo.put("evaluations", evaluation["id"], evaluation)
        repo.save_plot({**plot, "id": "plot-b", "project_id": "project-b"})
    with Session(engine) as session, session.begin():
        repo = Repository(session)
        copy = repo.get("plots", "plot-a")
        copy["geometry"]["coordinates"][0][0][0] = 0
        assert repo.get("plots", "plot-a") == plot
        assert repo.find_analysis("plot-a", 1, "fixture-v1") == analysis
        assert repo.find_analysis("plot-a", 1, "fixture-v2") is None
        assert repo.put("evaluations", evaluation["id"], evaluation) == evaluation
        with pytest.raises(ValueError, match="immutable"):
            repo.put("evaluations", evaluation["id"], {**evaluation, "margin_mad": "0.00"})
        repo.save_plot({**plot, "revision": 2})
        assert session.get(PlotRevisionRow, ("plot-a", 1)).payload == plot
        assert repo.get("evaluations", "evaluation-a") == evaluation
        assert repo.find_analysis("plot-a", 2, "fixture-v1") is None
        with pytest.raises(ValueError, match="exactly one revision"):
            repo.save_plot({**plot, "revision": 2})
        with pytest.raises(IntegrityError), session.begin_nested():
            repo.put(
                "plans",
                "cross-plot",
                {
                    "id": "cross-plot",
                    "plot_id": "plot-b",
                    "evaluation_id": "evaluation-a",
                    "revision": 1,
                },
            )
        with pytest.raises(IntegrityError), session.begin_nested():
            repo.put(
                "evaluations",
                "stale-analysis",
                {
                    "id": "stale-analysis",
                    "plot_id": "plot-a",
                    "plot_revision": 2,
                    "analysis_id": "analysis-a",
                },
            )
    engine.dispose()


@pytest.mark.postgis
def test_real_postgis_snapshot_round_trip():
    url = _postgis_test_url()
    engine = make_engine(url)
    create_schema(engine)
    key = uuid4().hex
    plot_id, analysis_id, evaluation_id, plan_id = (
        f"postgis-{prefix}-{key}" for prefix in ("p", "a", "e", "s")
    )
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [[-7.60, 33.00], [-7.59, 33.00], [-7.59, 33.01], [-7.60, 33.01], [-7.60, 33.00]]
        ],
    }
    plot = {
        "id": plot_id,
        "project_id": "postgis-test",
        "revision": 1,
        "name": "Synthetic integration plot",
        "geometry": geometry,
        "area_ha": "103.65",
    }
    analysis = {
        "id": analysis_id,
        "plot_id": plot_id,
        "plot_revision": 1,
        "fixture_version": "postgis-test-v1",
        "synthetic": True,
    }
    evaluation = {
        "id": evaluation_id,
        "plot_id": plot_id,
        "plot_revision": 1,
        "analysis_id": analysis_id,
        "margin_mad": "123.45",
        "synthetic": True,
    }
    plan = {
        "id": plan_id,
        "plot_id": plot_id,
        "evaluation_id": evaluation_id,
        "revision": 1,
        "assumptions": {"budget_mad": "5000.00"},
        "synthetic": True,
    }
    try:
        with Session(engine) as session, session.begin():
            repo = Repository(session)
            repo.save_plot(plot)
            repo.put("analyses", analysis_id, analysis)
            repo.put("evaluations", evaluation_id, evaluation)
            repo.put("plans", plan_id, plan)
        # A new connection/session after commit proves durable storage, not an
        # identity-map echo or a mock persistence adapter.
        with Session(engine) as session:
            repo = Repository(session)
            assert repo.get("plots", plot_id) == plot
            assert repo.get("evaluations", evaluation_id) == evaluation
            assert repo.get("plans", plan_id) == plan
            assert repo.find_analysis(plot_id, 1, "postgis-test-v1") == analysis
            srid, kind, square_meters, geojson = session.execute(
                text(
                    "SELECT ST_SRID(geometry), GeometryType(geometry), "
                    "ST_Area(geometry::geography), ST_AsGeoJSON(geometry) "
                    "FROM plot_revisions WHERE plot_id = :id AND revision = 1"
                ),
                {"id": plot_id},
            ).one()
            assert srid == 4326
            assert kind == "POLYGON"
            assert square_meters > 0
            assert json.loads(geojson) == geometry
            assert session.get(PlotRevisionRow, (plot_id, 1)).payload == plot
    finally:
        # Delete only the UUID-scoped rows created by this test. Existing local
        # development fixtures and records are never reset or truncated.
        with Session(engine) as session, session.begin():
            session.execute(delete(PlanRow).where(PlanRow.id == plan_id))
            session.execute(delete(EvaluationRow).where(EvaluationRow.id == evaluation_id))
            session.execute(delete(AnalysisRow).where(AnalysisRow.id == analysis_id))
            session.execute(delete(PlotRevisionRow).where(PlotRevisionRow.plot_id == plot_id))
            session.execute(delete(PlotRow).where(PlotRow.id == plot_id))
        engine.dispose()


@pytest.mark.postgis
def test_real_postgis_serializes_plan_edits_and_analysis_cache_creation():
    engine = make_engine(_postgis_test_url())
    create_schema(engine)
    key = uuid4().hex
    plot_id, plan_id = f"race-plot-{key}", f"race-plan-{key}"
    plot = {
        "id": plot_id,
        "project_id": key,
        "revision": 1,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-7.60, 33.00], [-7.59, 33.00], [-7.59, 33.01], [-7.60, 33.00]]],
        },
    }
    plan = {
        "id": plan_id,
        "plot_id": plot_id,
        "project_id": key,
        "evaluation_id": None,
        "revision": 1,
        "name": "Before concurrent changes",
    }
    try:
        with Session(engine) as session, session.begin():
            repo = Repository(session)
            repo.save_plot(plot)
            repo.put("plans", plan_id, plan)
        barrier = Barrier(2)

        def edit(name):
            try:
                with Session(engine) as session, session.begin():
                    repo = Repository(session)
                    before = repo.get("plans", plan_id)
                    barrier.wait(timeout=10)
                    repo.put("plans", plan_id, {**before, "revision": 2, "name": name})
                return "saved"
            except ValueError:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(edit, ["First edit", "Second edit"])) == ["saved", "stale"]
        with Session(engine) as session:
            assert Repository(session).get("plans", plan_id)["revision"] == 2

        barrier = Barrier(2)

        def analyze(_):
            with Session(engine) as session, session.begin():
                repo = Repository(session)
                barrier.wait(timeout=10)
                result = repo.find_analysis(plot_id, 1, "race-v1")
                if result is None:
                    result = {
                        "id": str(uuid4()),
                        "plot_id": plot_id,
                        "plot_revision": 1,
                        "fixture_version": "race-v1",
                    }
                    repo.put("analyses", result["id"], result)
                return result["id"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(analyze, range(2)))
        assert ids[0] == ids[1]
    finally:
        with Session(engine) as session, session.begin():
            session.execute(delete(PlanRow).where(PlanRow.id == plan_id))
            session.execute(delete(AnalysisRow).where(AnalysisRow.plot_id == plot_id))
            session.execute(delete(PlotRevisionRow).where(PlotRevisionRow.plot_id == plot_id))
            session.execute(delete(PlotRow).where(PlotRow.id == plot_id))
        engine.dispose()


@pytest.mark.postgis
def test_real_postgis_api_flow_survives_application_restart():
    url = _postgis_test_url()

    from fastapi.testclient import TestClient

    from agrix.api import create_app

    engine = make_engine(url)
    create_schema(engine)
    app = create_app(url)
    project_id = f"postgis-api-{uuid4().hex}"
    headers = {"X-Project-ID": project_id}
    plot_id = None
    second_app = None
    try:
        with TestClient(app) as client:
            response = client.post(
                "/plots",
                headers=headers,
                json={
                    "name": "Synthetic PostGIS API plot",
                    "analysis_mode": "demo",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-7.60, 33.00],
                                [-7.599, 33.00],
                                [-7.599, 33.001],
                                [-7.60, 33.001],
                                [-7.60, 33.00],
                            ]
                        ],
                    },
                    "constraints": {
                        "budget_mad": "100000.00",
                        "machinery_access": "available",
                        "labor_person_days_available": "100",
                        "previous_crops": [],
                    },
                },
            )
            assert response.status_code == 201, response.text
            plot = response.json()
            plot_id = plot["id"]
            response = client.post(
                f"/plots/{plot_id}/analyses", headers=headers, json={"plot_revision": 1}
            )
            assert response.status_code == 200, response.text
            analysis = response.json()
            assert analysis["status"] == "complete"
            response = client.post(
                "/rotations/suggest",
                headers=headers,
                json={
                    "plot_id": plot_id,
                    "plot_revision": 1,
                    "start_date": "2026-11-01",
                    "horizon_days": 730,
                },
            )
            assert response.status_code == 200, response.text
            candidate = response.json()["candidates"][0]
            response = client.post(
                "/rotations/evaluate", headers=headers, json=candidate["input_snapshot"]["request"]
            )
            assert response.status_code == 200, response.text
            evaluation = response.json()
            assert evaluation["fingerprint"] == candidate["fingerprint"]
            assert evaluation["financials"] == candidate["financials"]
            response = client.post(
                "/plans",
                headers=headers,
                json={
                    "plot_id": plot_id,
                    "evaluation_id": evaluation["id"],
                    "status": "selected",
                },
            )
            assert response.status_code == 201, response.text
            plan = response.json()
            response = client.post(
                "/chat",
                headers=headers,
                json={
                    "plot_id": plot_id,
                    "evaluation_ids": [evaluation["id"]],
                    "message": "شحال خاصني نوفر؟",
                    "locale": "ary",
                },
            )
            assert response.status_code == 200, response.text
            assert (
                response.json()["facts"][0]["peak_funding_need_mad"]
                == (evaluation["financials"]["whole_plot"]["peak_funding_need"]["value"])
            )
            response = client.patch(
                f"/plots/{plot_id}",
                headers=headers,
                json={"expected_revision": 1, "name": "Updated plot"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["revision"] == 2
        app.state.engine.dispose()
        # A fresh app creates its own engine/sessions. No in-memory request state
        # or ORM identity map is available to satisfy the following reads.
        second_app = create_app(url)
        with TestClient(second_app) as client:
            response = client.get(f"/plans/{plan['id']}", headers=headers)
            assert response.status_code == 200, response.text
            assert response.json() == plan
            assert (
                client.get(f"/evaluations/{evaluation['id']}", headers=headers).json() == evaluation
            )
            assert client.get(f"/plots/{plot_id}", headers=headers).json()["revision"] == 2
            response = client.post(
                f"/plots/{plot_id}/analyses", headers=headers, json={"plot_revision": 1}
            )
            assert response.status_code == 409, response.text
            response = client.patch(
                f"/plans/{plan['id']}",
                headers=headers,
                json={"expected_revision": 1, "name": "Updated plan"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["revision"] == 2
            response = client.patch(
                f"/plans/{plan['id']}",
                headers=headers,
                json={"expected_revision": 1, "name": "Stale edit"},
            )
            assert response.status_code == 409, response.text
    finally:
        app.state.engine.dispose()
        if second_app is not None:
            second_app.state.engine.dispose()
        if plot_id is not None:
            with Session(engine) as session, session.begin():
                session.execute(delete(PlanRow).where(PlanRow.plot_id == plot_id))
                session.execute(delete(EvaluationRow).where(EvaluationRow.plot_id == plot_id))
                session.execute(delete(AnalysisRow).where(AnalysisRow.plot_id == plot_id))
                session.execute(delete(PlotRevisionRow).where(PlotRevisionRow.plot_id == plot_id))
                session.execute(delete(PlotRow).where(PlotRow.id == plot_id))
        engine.dispose()
