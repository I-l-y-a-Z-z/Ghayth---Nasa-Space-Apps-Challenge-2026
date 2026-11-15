import copy
from datetime import date
from decimal import Decimal

import pytest

from agrix.cli import seed, smoke
from agrix.demo import demo_plot


def test_full_flow(client):
    result = smoke(client)
    assert result["status"] == "fits_current_inputs"
    assert result["duration_days"] == 525


def test_three_profile_coverages_and_statuses(client):
    expected = {
        "settat": ("complete", "fits_current_inputs"),
        "meknes": ("partial", "needs_confirmation"),
        "ouarzazate": ("complete", "needs_confirmation"),
    }
    for zone, (analysis_status, status) in expected.items():
        plot = client.post("/plots", json=demo_plot(zone).model_dump(mode="json")).json()
        analysis = client.post(f"/plots/{plot['id']}/analyses").json()
        assert analysis["status"] == analysis_status
        result = client.post(
            "/rotations/evaluate",
            json={
                "plot_id": plot["id"],
                "plot_revision": 1,
                "start_date": "2025-11-01",
                "sequence": [{"crop_id": "wheat_demo"}],
            },
        ).json()
        assert result["status"] == status
        assert all(
            e["synthetic"] and e["source_id"].startswith("fixture-") for e in result["evidence"]
        )
        if zone == "meknes":
            ph = analysis["profile"]["metrics"]["soil_ph"]
            assert ph["value"] is None and ph["availability"] == "missing"
        if zone == "ouarzazate":
            assert analysis["profile"]["rule_coverage"] == "unsupported"
            assert result["financials"]["whole_plot"]["expected_revenue"]["value"] is None
            assert result["market_history"]["availability"] == "missing"


def test_unknown_fixture_coverage_remains_unknown(client):
    payload = demo_plot().model_dump(mode="json")
    payload["geometry"]["coordinates"] = [
        [[-9.0, 31.0], [-8.999, 31.0], [-8.999, 31.001], [-9.0, 31.001], [-9.0, 31.0]]
    ]
    p = client.post("/plots", json=payload).json()
    analysis = client.post(f"/plots/{p['id']}/analyses").json()
    assert analysis["status"] == "unsupported"
    assert analysis["profile"]["zone_id"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("budget_mad", "0.00"),
        ("machinery_access", "unavailable"),
        ("labor_person_days_available", "0"),
    ],
)
def test_constraints_change_same_evaluation_path(client, evaluation_request, field, value):
    original = client.post("/rotations/evaluate", json=evaluation_request).json()
    assert original["status"] == "fits_current_inputs"
    changed = copy.deepcopy(original["input_snapshot"]["request"])
    changed["constraints"][field] = value
    result = client.post("/rotations/evaluate", json=changed).json()
    assert result["status"] == "doesnt_fit"
    assert result["fingerprint"] != original["fingerprint"]
    old = client.get(f"/evaluations/{original['id']}").json()
    assert old == original


def test_unknown_constraints_do_not_become_zero(client, evaluation_request):
    evaluation_request["constraints"] = {}
    result = client.post("/rotations/evaluate", json=evaluation_request).json()
    assert result["status"] == "needs_confirmation"
    assert result["input_snapshot"]["request"]["constraints"]["budget_mad"] is None
    assert any(
        c["reason"]["code"] == "BUDGET_CHECK" and c["outcome"] == "unknown"
        for c in result["checks"]
    )


def test_suggestion_custom_exact_parity_and_duration(client, plot):
    response = client.post(
        "/rotations/suggest",
        json={
            "plot_id": plot["id"],
            "plot_revision": 1,
            "start_date": "2024-11-01",
            "horizon_days": 730,
        },
    )
    assert response.status_code == 200
    for suggested in response.json()["candidates"]:
        custom = client.post(
            "/rotations/evaluate", json=suggested["input_snapshot"]["request"]
        ).json()
        for key in (
            "fingerprint",
            "status",
            "checks",
            "rotation",
            "financials",
            "timeline",
            "cash_events",
            "market_history",
            "labor",
            "machinery",
            "input_snapshot",
        ):
            assert custom[key] == suggested[key]
        r = custom["rotation"]
        assert (
            r["duration_days"]
            == (date.fromisoformat(r["end_date"]) - date.fromisoformat(r["start_date"])).days + 1
        )


def test_stale_revisions_saved_snapshot_and_analysis_reuse(client, plot, evaluation_request):
    a = client.post(f"/plots/{plot['id']}/analyses").json()
    assert client.post(f"/plots/{plot['id']}/analyses").json()["id"] == a["id"]
    e = client.post("/rotations/evaluate", json=evaluation_request).json()
    plan = client.post(
        "/plans", json={"plot_id": plot["id"], "evaluation_id": e["id"], "status": "selected"}
    ).json()
    changed = copy.deepcopy(plot["geometry"])
    changed["coordinates"][0][1][0] += 0.0001
    changed["coordinates"][0][2][0] += 0.0001
    revision = client.patch(
        f"/plots/{plot['id']}", json={"expected_revision": 1, "geometry": changed}
    )
    assert revision.status_code == 200
    assert revision.json()["revision"] == 2
    assert client.post(f"/plots/{plot['id']}/analyses").json()["id"] != a["id"]
    for response in [
        client.post("/rotations/evaluate", json=evaluation_request),
        client.post(f"/plots/{plot['id']}/analyses", json={"plot_revision": 1}),
        client.post("/plans", json={"plot_id": plot["id"], "evaluation_id": e["id"]}),
        client.patch(f"/plots/{plot['id']}", json={"expected_revision": 1, "name": "stale"}),
    ]:
        assert response.status_code == 409
    assert client.get(f"/plans/{plan['id']}").json()["evaluation_snapshot"] == e
    assert client.get(f"/analyses/{a['id']}").json() == a
    reply = client.post(
        "/chat",
        json={
            "plot_id": plot["id"],
            "evaluation_ids": [e["id"]],
            "message": "Explain",
            "locale": "en",
        },
    ).json()
    assert "older plot revision" in reply["message"]


def test_plan_edit_reevaluation_and_draft(client, evaluation_request):
    original = client.post("/rotations/evaluate", json=evaluation_request).json()
    plan = client.post(
        "/plans", json={"plot_id": original["plot_id"], "evaluation_id": original["id"]}
    ).json()
    request = copy.deepcopy(original["input_snapshot"]["request"])
    request["constraints"]["payment_lag_days"] = 100
    updated_eval = client.post("/rotations/evaluate", json=request).json()
    changed = client.patch(
        f"/plans/{plan['id']}",
        json={"expected_revision": 1, "evaluation_id": updated_eval["id"], "status": "selected"},
    ).json()
    assert changed["revision"] == 2 and changed["assumptions"]["payment_lag_days"] == 100
    assert client.get(f"/evaluations/{original['id']}").json() == original
    assert (
        client.patch(
            f"/plans/{plan['id']}", json={"expected_revision": 1, "name": "old"}
        ).status_code
        == 409
    )
    draft = client.post("/plans", json={"plot_id": original["plot_id"], "draft": request}).json()
    assert (
        draft["evaluation_id"] is None and draft["draft"]["constraints"] == request["constraints"]
    )
    assert (
        client.patch(
            f"/plans/{draft['id']}", json={"expected_revision": 1, "status": "selected"}
        ).status_code
        == 422
    )


@pytest.mark.parametrize("locale", ["ary", "fr", "en"])
def test_chat_numeric_parity_and_read_only(client, evaluation_request, locale):
    evaluation = client.post("/rotations/evaluate", json=evaluation_request).json()
    plan = client.post(
        "/plans", json={"plot_id": evaluation["plot_id"], "evaluation_id": evaluation["id"]}
    ).json()
    reply = client.post(
        "/chat",
        json={
            "plot_id": evaluation["plot_id"],
            "evaluation_ids": [evaluation["id"]],
            "message": "What if budget changes?",
            "locale": locale,
        },
    ).json()
    peak = evaluation["financials"]["whole_plot"]["peak_funding_need"]["value"]
    assert reply["facts"][0]["peak_funding_need_mad"] == peak
    assert peak in reply["message"] and reply["read_only"]
    if locale == "ary":
        assert "درهم" in reply["message"]
    assert client.get(f"/plans/{plan['id']}").json() == plan


def test_cross_plot_and_project_isolation(client, plot, evaluation_request):
    e = client.post("/rotations/evaluate", json=evaluation_request).json()
    other = client.post("/plots", json=demo_plot().model_dump(mode="json")).json()
    assert (
        client.post("/plans", json={"plot_id": other["id"], "evaluation_id": e["id"]}).status_code
        == 422
    )
    assert (
        client.post(
            "/chat",
            json={"plot_id": other["id"], "evaluation_ids": [e["id"]], "message": "Explain"},
        ).status_code
        == 422
    )
    for path in (f"/plots/{plot['id']}", f"/evaluations/{e['id']}"):
        assert client.get(path, headers={"X-Project-ID": "other-project"}).status_code == 404


def test_unknown_ids_and_invalid_geometry(client):
    for path in ("/plots/missing", "/plans/missing", "/evaluations/missing", "/analyses/missing"):
        response = client.get(path)
        assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"
    payload = demo_plot().model_dump(mode="json")
    payload["geometry"]["coordinates"] = [
        [[-7.6, 33], [-7.59, 33.01], [-7.59, 33], [-7.6, 33.01], [-7.6, 33]]
    ]
    assert client.post("/plots", json=payload).status_code == 422


def test_invalid_sequence_and_short_horizon(client, evaluation_request):
    for sequence in (
        [],
        [{"crop_id": "unknown"}],
        [{"crop_id": "wheat_demo", "start_date": "2025-11-10", "end_date": "2025-11-01"}],
    ):
        assert (
            client.post(
                "/rotations/evaluate", json={**evaluation_request, "sequence": sequence}
            ).status_code
            == 422
        )
    request = {k: v for k, v in evaluation_request.items() if k != "sequence"}
    request["horizon_days"] = 1
    suggestions = client.post("/rotations/suggest", json=request).json()
    assert suggestions["candidates"] == [] and suggestions["reasons"]
    short = client.post(
        "/rotations/evaluate", json={**evaluation_request, "horizon_days": 1}
    ).json()
    assert short["status"] == "doesnt_fit"


def test_conflicts_and_zero_sales(client, evaluation_request):
    request = copy.deepcopy(evaluation_request)
    request["sequence"] = [
        {"crop_id": "wheat_demo", "start_date": "2025-11-01"},
        {"crop_id": "barley_demo", "start_date": "2025-11-02"},
    ]
    result = client.post("/rotations/evaluate", json=request).json()
    assert result["status"] == "doesnt_fit"
    codes = {c["reason"]["code"] for c in result["checks"] if c["outcome"] == "fail"}
    assert {"SEASON_OVERLAP_CHECK", "PREDECESSOR_CHECK"} <= codes
    request = copy.deepcopy(evaluation_request)
    request["constraints"] = {
        **demo_plot().constraints.model_dump(mode="json"),
        "sale_price_mad_per_kg": {"wheat_demo": "0.00"},
    }
    result = client.post("/rotations/evaluate", json=request).json()
    revenue = result["financials"]["whole_plot"]["expected_revenue"]
    assert revenue["value"] == "0.00" and revenue["availability"] == "available"
    assert Decimal(result["financials"]["whole_plot"]["operating_margin"]["value"]) < 0


def test_seed_idempotence_and_openapi(client, tmp_path):
    from agrix.db import create_schema, make_engine

    url = "sqlite+pysqlite:///" + str(tmp_path / "seed.sqlite3")
    engine = make_engine(url)
    create_schema(engine)
    assert seed(url) == seed(url)
    engine.dispose()
    schema = client.get("/openapi.json").json()
    names = {
        name.removesuffix("-Input").removesuffix("-Output")
        for name in schema["components"]["schemas"]
    }
    assert {
        "Plot",
        "PlotProfile",
        "FarmConstraints",
        "CropProfile",
        "Rotation",
        "Scenario",
        "Evaluation",
        "Operation",
        "CashEvent",
        "Evidence",
    } <= names
    assert {"/chat", "/workspace", "/demo/seed"} <= schema["paths"].keys()
