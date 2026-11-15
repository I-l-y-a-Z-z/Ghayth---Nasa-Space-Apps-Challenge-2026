"""Regression cases found by independent contract and boundary review."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from agrix.api import create_app
from agrix.catalog import load_catalog
from agrix.evaluator import evaluate
from agrix.geometry import normalize_geometry
from agrix.models import EvaluationRequest, FarmConstraints, Plot, Polygon
from agrix.profiles import get_profile


def _geometry():
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [-7.60, 33.0],
                [-7.599, 33.0],
                [-7.599, 33.001],
                [-7.60, 33.001],
                [-7.60, 33.0],
            ]
        ],
    }


def _constraints():
    return {
        "budget_mad": "1000000.00",
        "machinery_access": "available",
        "labor_person_days_available": "100",
        "previous_crops": [],
    }


@pytest.fixture
def reviewed_client():
    app = create_app("sqlite://", initialize=True, default_analysis_mode="demo")
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.state.engine.dispose()


def _plot(client):
    response = client.post("/plots", json={"geometry": _geometry(), "constraints": _constraints()})
    assert response.status_code == 201, response.text
    return response.json()


def _request(plot):
    return {
        "plot_id": plot["id"],
        "plot_revision": plot["revision"],
        "start_date": "2025-11-01",
        "horizon_days": 730,
        "sequence": [{"crop_id": "wheat_demo"}],
    }


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_request_errors_remain_json_422(reviewed_client, nonfinite):
    payload = _request(_plot(reviewed_client))
    payload["constraints"] = {**_constraints(), "labor_person_days_available": nonfinite}
    response = reviewed_client.post(
        "/rotations/evaluate",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("endpoint", ["/rotations/evaluate", "/rotations/suggest"])
def test_planning_date_overflow_is_client_error(reviewed_client, endpoint):
    payload = _request(_plot(reviewed_client))
    payload["start_date"] = "9999-12-31"
    if endpoint.endswith("suggest"):
        payload.pop("sequence")
    response = reviewed_client.post(endpoint, json=payload)
    assert response.status_code == 422, response.text


def test_explicit_crop_date_overflow_is_client_error(reviewed_client):
    payload = _request(_plot(reviewed_client))
    payload["sequence"] = [{"crop_id": "wheat_demo", "start_date": "9999-12-31"}]
    response = reviewed_client.post("/rotations/evaluate", json=payload)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "extra",
    [
        {"wage_mad_per_person_day": "999999999999.99"},
        {"yield_kg_per_ha": {"wheat_demo": "999999999999"}},
    ],
)
def test_computed_amount_overflow_is_client_error(reviewed_client, extra):
    payload = _request(_plot(reviewed_client))
    payload["constraints"] = {**_constraints(), **extra}
    response = reviewed_client.post("/rotations/evaluate", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "EVALUATION_OUT_OF_RANGE"


def test_truncated_crop_does_not_generate_operations_after_its_harvest(reviewed_client):
    payload = _request(_plot(reviewed_client))
    payload["sequence"] = [
        {"crop_id": "wheat_demo", "start_date": "2025-11-01", "end_date": "2025-11-02"}
    ]
    response = reviewed_client.post("/rotations/evaluate", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "CROP_DURATION_CONFLICT"


def test_ambiguous_saved_inputs_and_empty_plan_patch_are_rejected(reviewed_client):
    plot = _plot(reviewed_client)
    request = _request(plot)
    response = reviewed_client.post("/rotations/evaluate", json=request)
    assert response.status_code == 200, response.text
    evaluation = response.json()
    ambiguous = reviewed_client.post(
        "/plans",
        json={
            "plot_id": plot["id"],
            "status": "selected",
            "evaluation_id": evaluation["id"],
            "draft": request,
        },
    )
    assert ambiguous.status_code == 422, ambiguous.text
    saved = reviewed_client.post(
        "/plans", json={"plot_id": plot["id"], "evaluation_id": evaluation["id"]}
    )
    assert saved.status_code == 201, saved.text
    empty_patch = reviewed_client.patch(
        f"/plans/{saved.json()['id']}", json={"expected_revision": 1}
    )
    assert empty_patch.status_code == 422, empty_patch.text


@pytest.mark.parametrize("status", ["draft", "selected"])
def test_empty_evaluation_id_cannot_crash_plan_creation(reviewed_client, status):
    plot = _plot(reviewed_client)
    response = reviewed_client.post(
        "/plans", json={"plot_id": plot["id"], "evaluation_id": "", "status": status}
    )
    assert response.status_code in {404, 422}, response.text


def test_saved_snapshot_survives_plot_changes_but_cannot_be_silently_reselected(reviewed_client):
    plot = _plot(reviewed_client)
    result = reviewed_client.post("/rotations/evaluate", json=_request(plot))
    assert result.status_code == 200, result.text
    evaluation = result.json()
    result = reviewed_client.post(
        "/plans",
        json={"plot_id": plot["id"], "evaluation_id": evaluation["id"], "status": "selected"},
    )
    assert result.status_code == 201, result.text
    plan = result.json()
    changed = reviewed_client.patch(
        f"/plots/{plot['id']}", json={"expected_revision": 1, "name": "Changed plot"}
    )
    assert changed.status_code == 200, changed.text
    old_evaluation = reviewed_client.get(f"/evaluations/{evaluation['id']}")
    assert old_evaluation.json() == evaluation
    old_plan = reviewed_client.get(f"/plans/{plan['id']}")
    assert old_plan.json()["evaluation_snapshot"] == evaluation
    assert old_plan.json()["plot_revision"] == 1
    reselected = reviewed_client.patch(
        f"/plans/{plan['id']}", json={"expected_revision": 1, "status": "selected"}
    )
    assert reselected.status_code == 409, reselected.text
    foreign_project = reviewed_client.get(
        f"/evaluations/{evaluation['id']}", headers={"X-Project-ID": "other-project"}
    )
    assert foreign_project.status_code == 404


def test_suggestion_and_explicit_custom_dates_have_equivalent_snapshots(reviewed_client):
    plot = _plot(reviewed_client)
    request = _request(plot)
    request.pop("sequence")
    response = reviewed_client.post("/rotations/suggest", json=request)
    assert response.status_code == 200, response.text
    suggested = response.json()["candidates"][0]
    request["sequence"] = [
        {key: crop[key] for key in ("crop_id", "start_date", "end_date")}
        for crop in suggested["rotation"]["crops"]
    ]
    response = reviewed_client.post("/rotations/evaluate", json=request)
    assert response.status_code == 200, response.text
    custom = response.json()
    for key in ("input_snapshot", "fingerprint", "rotation", "financials", "cash_events", "checks"):
        assert custom[key] == suggested[key], key


def test_scenario_evidence_references_are_resolved_or_rejected(reviewed_client):
    request = _request(_plot(reviewed_client))
    request["scenario"] = {"evidence_ids": ["unregistered-evidence"]}
    response = reviewed_client.post("/rotations/evaluate", json=request)
    if response.status_code == 422:
        return
    assert response.status_code == 200, response.text
    evaluation = response.json()
    known = {item["id"] for item in evaluation["evidence"]}
    referenced = {
        evidence_id for event in evaluation["cash_events"] for evidence_id in event["evidence_ids"]
    }
    assert referenced <= known


def test_pure_evaluation_snapshot_is_independent_of_mutable_inputs():
    geometry, area = normalize_geometry(Polygon.model_validate(_geometry()))
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    plot = Plot(
        id="pure-plot",
        project_id="demo",
        name="Pure fixture",
        revision=1,
        geometry=geometry,
        area_ha=area,
        constraints=FarmConstraints.model_validate(_constraints()),
        created_at=now,
        updated_at=now,
    )
    catalog = load_catalog()
    profile = get_profile(plot)
    request = EvaluationRequest.model_validate(_request(plot.model_dump(mode="json")))
    result = evaluate(plot, profile, request, catalog, analysis_id="pure-analysis")
    original_snapshot = result.input_snapshot.model_dump(mode="json")
    catalog.version = "modified-after-evaluation"
    profile.metrics["rainfall_mm"].value = Decimal(0)
    plot.constraints.budget_mad = Decimal(0)
    request.scenario.price_multiplier = Decimal(0)
    assert result.input_snapshot.model_dump(mode="json") == original_snapshot
