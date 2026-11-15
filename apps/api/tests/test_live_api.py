"""Live job lifecycle with deterministic providers, file SQLite and real HTTP contracts."""

import time
from datetime import date
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from agrix.api import create_app
from agrix.config import Settings
from agrix.db import Repository
from agrix.demo import demo_plot
from agrix.jobs import pending_profile
from agrix.models import Availability, Evidence, LayerCoverage, Metric, Origin, Reason


class Pipeline:
    entered = None
    release = None
    fail = False
    calls = []

    def __init__(self, cache_dir, network_enabled):
        self.progress_callback = None

    def run(self, plot, start, end, previous_profile=None, retry_layers=None):
        self.calls.append((plot.revision, start, end, previous_profile, retry_layers))
        if self.entered:
            self.entered.set()
            assert self.release.wait(10), "Test did not release provider"
        if self.fail:
            raise RuntimeError("secret-bearing-provider-message-must-not-leak")
        profile = pending_profile(plot, "test-provider-v1")
        evidence = Evidence(
            id="public-test",
            source_id="NASA_POWER_DAILY",
            source_version="test",
            synthetic=False,
            origin=Origin.modeled,
        )
        profile.evidence = [evidence]
        profile.metrics["rainfall_mm"] = Metric(
            value=100,
            unit="mm",
            availability=Availability.partial,
            origin=Origin.modeled,
            evidence_ids=[evidence.id],
            reason=Reason(code="TEST_PERIOD_PARTIAL"),
        )
        profile.layers[0] = LayerCoverage(
            id="rainfall",
            availability=Availability.partial,
            state="partial",
            evidence_ids=[evidence.id],
            reason=Reason(code="TEST_PERIOD_PARTIAL"),
        )
        self.progress_callback(profile.layers[0])
        return profile


@pytest.fixture
def live(tmp_path):
    Pipeline.entered = Pipeline.release = None
    Pipeline.fail = False
    Pipeline.calls = []
    settings = Settings(
        database_url="sqlite:///" + str(tmp_path / "live.sqlite3"),
        cache_directory=tmp_path / "cache",
        network_enabled=False,
        analysis_start=date(2024, 10, 1),
        analysis_end=date(2024, 10, 31),
    )
    app = create_app(settings=settings, initialize=True, pipeline_factory=Pipeline)
    with TestClient(app) as client:
        yield client, app


def create_live(client):
    payload = demo_plot().model_dump(mode="json")
    payload.pop("analysis_mode")
    payload["constraints"] = {}
    return client.post("/plots", json=payload).json()


def completed(client, id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = client.get("/analyses/" + id).json()
        if result["status"] not in {"pending", "running"}:
            return result
        time.sleep(0.02)
    pytest.fail("Analysis did not finish")


def test_live_default_jobs_cache_retry_and_immutable_snapshots(live):
    client, app = live
    plot = create_live(client)
    assert plot["analysis_mode"] == "live"
    assert plot["constraints"]["setup_cost_mad"] is None
    assert plot["constraints"]["wage_mad_per_person_day"] is None
    Pipeline.entered, Pipeline.release = Event(), Event()
    job = client.post(f"/plots/{plot['id']}/analyses", json={"plot_revision": 1}).json()
    try:
        assert job["status"] == "pending" and not job["synthetic"]
        assert Pipeline.entered.wait(5)
        assert client.get("/analyses/" + job["id"]).json()["status"] == "running"
        assert client.post(f"/plots/{plot['id']}/analyses").json()["id"] == job["id"]
        blocked = client.post(
            "/rotations/evaluate",
            json={
                "plot_id": plot["id"],
                "plot_revision": 1,
                "start_date": "2026-11-01",
                "sequence": [{"crop_id": "wheat"}],
            },
        )
        assert blocked.status_code == 409
    finally:
        Pipeline.release.set()
    original = completed(client, job["id"])
    assert original["status"] == "partial"
    retry = client.post(
        "/analyses/" + job["id"] + "/retry", json={"retry_layers": ["terrain"]}
    ).json()
    assert retry["id"] != original["id"]
    completed(client, retry["id"])
    assert client.get("/analyses/" + original["id"]).json() == original
    assert Pipeline.calls[-1][3] is not None
    assert Pipeline.calls[-1][4] == ["terrain"]
    with Session(app.state.engine) as session, session.begin():
        with pytest.raises(ValueError, match="immutable"):
            Repository(session).transition_analysis(original)
    monitor = client.get("/workspace/ingestion").json()
    assert monitor["counts"]["partial"] == 2
    assert not client.get("/workspace/ingestion", headers={"X-Project-ID": "other"}).json()["jobs"]


def test_real_evaluation_parity_missing_costs_sources_and_input_defaults(live):
    client, _ = live
    plot = create_live(client)
    initial = client.post(
        "/rotations/evaluate",
        json={
            "plot_id": plot["id"],
            "plot_revision": 1,
            "start_date": "2026-11-01",
            "sequence": [{"crop_id": "wheat"}],
        },
    )
    assert initial.status_code == 409 and initial.json()["error"]["code"] == "ANALYSIS_REQUIRED"
    job = client.post(f"/plots/{plot['id']}/analyses").json()
    completed(client, job["id"])
    response = client.post(
        "/rotations/suggest",
        json={
            "plot_id": plot["id"],
            "plot_revision": 1,
            "start_date": "2026-11-01",
            "constraints": {},
        },
    )
    assert response.status_code == 200, response.text
    candidate = response.json()["candidates"][0]
    assert not candidate["synthetic"]
    assert candidate["input_snapshot"]["request"]["scenario"]["id"] == "historical-baseline-v1"
    assert candidate["input_snapshot"]["request"]["constraints"]["setup_cost_mad"] == "0.00"
    assert candidate["economics"]["basis"] == "published_morocco_references"
    assert candidate["financials"]["whole_plot"]["operating_cost"]["value"] is not None
    assert candidate["financials"]["whole_plot"]["expected_revenue"]["value"] is not None
    replay = client.post("/rotations/evaluate", json=candidate["input_snapshot"]["request"]).json()
    assert replay["fingerprint"] == candidate["fingerprint"]
    chat = client.post(
        "/chat",
        json={
            "plot_id": plot["id"],
            "evaluation_ids": [candidate["id"]],
            "message": "Explain funding",
            "locale": "en",
        },
    ).json()
    assert not chat["synthetic"]
    assert (
        chat["facts"][0]["peak_funding_need_mad"]
        == candidate["financials"]["whole_plot"]["peak_funding_need"]["value"]
    )
    saved = client.post(
        "/plans", json={"plot_id": plot["id"], "evaluation_id": candidate["id"]}
    ).json()
    assert not saved["synthetic"]
    assert client.get("/plans/" + saved["id"]).json()["evaluation_snapshot"] == candidate


def test_failed_jobs_sanitize_provider_errors_and_revision_guard(live):
    client, _ = live
    plot = create_live(client)
    Pipeline.fail = True
    job = client.post(f"/plots/{plot['id']}/analyses").json()
    result = completed(client, job["id"])
    assert result["status"] == "failed"
    assert "secret-bearing" not in str(result)
    client.patch("/plots/" + plot["id"], json={"expected_revision": 1, "name": "Revised field"})
    assert client.post("/analyses/" + job["id"] + "/retry").status_code == 409
    assert (
        client.post(
            f"/plots/{plot['id']}/analyses", json={"retry_layers": ["not-a-layer"]}
        ).status_code
        == 422
    )


def test_soil_test_depth_and_units_override_only_matching_properties(live):
    client, _ = live
    payload = demo_plot().model_dump(mode="json")
    payload["analysis_mode"] = "live"
    payload["constraints"] = {
        "soil_tests": [
            {
                "property": "soil_ph",
                "value": "7.1",
                "unit": "pH",
                "depth_top_cm": 0,
                "depth_bottom_cm": 20,
                "sample_date": "2025-03-01",
                "source_label": "Local laboratory",
            },
            {
                "property": "soil_moisture",
                "value": "20",
                "unit": "%",
                "depth_top_cm": 0,
                "depth_bottom_cm": 5,
                "sample_date": "2025-03-01",
                "source_label": "Field measurement",
            },
        ]
    }
    plot = client.post("/plots", json=payload).json()
    job = client.post(f"/plots/{plot['id']}/analyses").json()
    profile = completed(client, job["id"])["profile"]
    assert profile["metrics"]["soil_ph"]["value"] == "7.1"
    assert profile["metrics"]["soil_ph"]["depth_cm"] == [0, 20]
    assert profile["metrics"]["soil_moisture_surface"]["value"] == "0.20"
    assert "soil_moisture" not in profile["metrics"]
    payload["constraints"]["soil_tests"][0]["value"] = "99"
    assert client.post("/plots", json=payload).status_code == 422


@pytest.mark.parametrize("system", ["unknown", "rainfed", "irrigated"])
def test_farmer_can_generate_without_financial_inputs_or_a_save_step(live, system):
    client, _ = live
    plot = create_live(client)
    job = client.post(f"/plots/{plot['id']}/analyses").json()
    completed(client, job["id"])
    response = client.post(
        "/rotations/suggest",
        json={
            "plot_id": plot["id"],
            "plot_revision": 1,
            "start_date": "2026-11-15",
            "horizon_days": 730,
            "constraints": {"production_system": system},
        },
    )
    assert response.status_code == 200, response.text
    candidates = response.json()["candidates"]
    assert candidates
    paths = [
        tuple(c["crop_id"] for c in e["rotation"]["crops"] if c["crop_id"] != "fallow")
        for e in candidates
    ]
    assert len(paths) == len(set(paths))
    assert len({tuple(sorted(path)) for path in paths[:3]}) == min(3, len(paths))
    for candidate in candidates:
        assert candidate["input_snapshot"]["request"]["constraints"]["production_system"] == system
        assert candidate["input_snapshot"]["request"]["constraints"]["budget_mad"] is None
        assert candidate["economics"]["references"]
        assert candidate["financials"]["whole_plot"]["expected_revenue"]["value"] is not None
        productive = [c for c in candidate["rotation"]["crops"] if c["crop_id"] != "fallow"]
        families = {c["id"]: c["family"] for c in candidate["input_snapshot"]["catalog"]["crops"]}
        assert all(
            families[a["crop_id"]] != families[b["crop_id"]]
            for a, b in zip(productive, productive[1:], strict=False)
        )
        assert candidate["status"] == "needs_confirmation"
    # Recommendation inputs never silently change the saved farmer record.
    assert client.get("/plots/" + plot["id"]).json()["revision"] == 1
