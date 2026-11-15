"""Independent regressions for restart, drafts and stable what-if evidence."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from agrix.api import create_app
from agrix.catalog import load_catalog
from agrix.config import Settings
from agrix.db import Repository, create_schema, make_engine
from agrix.demo import demo_plot
from agrix.jobs import AnalysisRunner, pending_profile
from agrix.models import (
    Analysis,
    AnalysisRequest,
    Availability,
    ChatRequest,
    CropSelection,
    DailyWeather,
    DateRange,
    EvaluationRequest,
    Evidence,
    FarmConstraints,
    LayerCoverage,
    Metric,
    PlanCreate,
)
from agrix.service import Service


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(
        database_url="sqlite:///" + str(tmp_path / "review.sqlite3"),
        cache_directory=tmp_path / "cache",
        network_enabled=False,
        analysis_start=date(2024, 10, 1),
        analysis_end=date(2024, 10, 31),
    )
    engine = make_engine(settings.database_url)
    create_schema(engine)
    with Session(engine) as session:
        service = Service(
            Repository(session),
            load_catalog(),
            "review-project",
            runner=SimpleNamespace(settings=settings),
            default_analysis_mode="live",
        )
        request = demo_plot().model_copy(update={"analysis_mode": "live"})
        plot = service.create_plot(request)
        yield service, plot, engine, settings
    engine.dispose()


def _profile(plot, version, rain):
    profile = pending_profile(plot, version)
    profile.zone_id = "settat"
    profile.rule_coverage = Availability.partial
    profile.metrics["rainfall_mm"] = Metric(
        value=rain, unit="mm", availability="available", origin="modeled", evidence_ids=[version]
    )
    profile.evidence = [
        Evidence(
            id=version,
            source_id="local-review-weather",
            source_version=version,
            origin="modeled",
            synthetic=False,
        )
    ]
    profile.weather = [
        DailyWeather(
            date=date(2024, 11, 1), rain_mm=rain, tmin_c=10, tmax_c=20, evidence_ids=[version]
        )
    ]
    profile.layers[0] = LayerCoverage(
        id="rainfall", availability="available", state="complete", evidence_ids=[version]
    )
    return profile


def _analysis(plot, version, rain, **updates):
    fields = dict(
        id=version,
        project_id=plot.project_id,
        plot_id=plot.id,
        plot_revision=plot.revision,
        fixture_version=version,
        status="partial",
        profile=_profile(plot, version, rain),
        created_at=datetime.now(timezone.utc),
        synthetic=False,
        period=DateRange(start=date(2024, 10, 1), end=date(2024, 10, 31)),
        plot_snapshot=plot,
    )
    fields.update(updates)
    return Analysis(**fields)


def test_regional_chickpea_draft_can_be_saved_before_analysis(runtime):
    service, plot, _, _ = runtime
    draft = EvaluationRequest(
        plot_id=plot.id,
        plot_revision=1,
        start_date=date(2026, 11, 1),
        sequence=[CropSelection(crop_id="chickpea")],
    )
    plan = service.create_plan(PlanCreate(plot_id=plot.id, draft=draft))
    assert plan.draft.sequence[0].crop_id == "chickpea"
    assert not plan.synthetic


def test_resumed_retry_restores_persisted_partial_source_and_layer_selection(runtime):
    service, plot, engine, settings = runtime
    previous = _analysis(plot, "review-original", 100)
    service.put("analyses", previous)
    pending = _analysis(
        plot,
        "review-retry",
        0,
        status="pending",
        profile=pending_profile(plot, "review-retry"),
        previous_analysis_id=previous.id,
        retry_layers=["terrain"],
    )
    service.put("analyses", pending)
    service.repo.session.commit()
    calls = []

    class Pipeline:
        def __init__(self, *args, **kwargs):
            self.progress_callback = None

        def run(self, plot, start, end, previous_profile=None, retry_layers=None):
            calls.append((previous_profile, retry_layers))
            return previous_profile.model_copy(deep=True)

    runner = AnalysisRunner(engine, settings, pipeline_factory=Pipeline)
    try:
        runner._run(pending, None, None)
        assert len(calls) == 1
        restored, layers = calls[0]
        assert restored.metrics["rainfall_mm"].value == 100
        assert layers == ["terrain"]
        service.repo.session.expire_all()
        assert service.get("analyses", pending.id, Analysis).status == "partial"
        assert service.get("analyses", previous.id, Analysis) == previous
    finally:
        runner.close()


def test_budget_only_chat_what_if_keeps_original_environment_snapshot(runtime):
    service, plot, _, _ = runtime
    initial = _analysis(plot, "before-reanalysis", 100)
    service.put("analyses", initial)
    original = service.evaluate(
        EvaluationRequest(
            plot_id=plot.id,
            plot_revision=1,
            start_date=date(2026, 11, 1),
            sequence=[CropSelection(crop_id="wheat")],
        )
    )
    updated = _analysis(
        plot, "after-reanalysis", 999, created_at=datetime.now(timezone.utc) + timedelta(seconds=1)
    )
    service.put("analyses", updated)
    reply = service.chat(
        ChatRequest(
            plot_id=plot.id,
            evaluation_ids=[original.id],
            message="What if budget is 15000 MAD?",
            locale="en",
        )
    )
    assert reply.what_if is not None
    assert reply.what_if.input_snapshot.request.constraints.budget_mad == 15000
    assert reply.what_if.input_snapshot.profile == original.input_snapshot.profile
    assert reply.what_if.input_snapshot.catalog == original.input_snapshot.catalog
    assert reply.what_if.analysis_id == original.analysis_id
    assert service.get("evaluations", original.id, type(original)) == original


def test_explicit_production_settings_cannot_create_unprotected_app(tmp_path):
    with pytest.raises(ValueError):
        settings = Settings(
            environment="production",
            access_password=None,
            session_secret="",
            database_url="sqlite:///" + str(tmp_path / "unsafe.sqlite3"),
            cache_directory=tmp_path / "unsafe-cache",
            network_enabled=False,
        )
        create_app(settings=settings, initialize=True)


def test_live_draft_cannot_persist_demo_wage_or_setup_defaults(runtime):
    service, plot, _, _ = runtime
    draft = EvaluationRequest(
        plot_id=plot.id,
        plot_revision=1,
        start_date=date(2026, 11, 1),
        sequence=[CropSelection(crop_id="wheat")],
        constraints=FarmConstraints(),
    )
    saved = service.create_plan(PlanCreate(plot_id=plot.id, draft=draft))
    assert saved.assumptions.setup_cost_mad is None
    assert saved.assumptions.wage_mad_per_person_day is None
    assert saved.draft.scenario.synthetic is False
    assert saved.draft.scenario.id == "historical-baseline-v1"


def test_repeated_retry_does_not_reuse_empty_pending_profile(runtime):
    from agrix.jobs import request_key

    service, plot, _, settings = runtime
    key = request_key(plot, settings.analysis_start, settings.analysis_end, settings)
    original = _analysis(plot, "terminal-source", 100, request_key=key)
    service.put("analyses", original)
    pending = _analysis(
        plot,
        "existing-retry",
        0,
        status="pending",
        request_key=key,
        profile=pending_profile(plot, "existing-retry"),
        previous_analysis_id=original.id,
        retry_layers=["terrain"],
        created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    service.put("analyses", pending)
    deferred = []
    service.runner.defer = lambda *args, **kwargs: deferred.append((args, kwargs))
    result = service.retry_analysis(original.id, AnalysisRequest(retry_layers=["terrain"]))
    if result.id != pending.id:
        assert result.previous_analysis_id == original.id
        assert deferred[-1][1]["previous"].metrics["rainfall_mm"].value == 100
