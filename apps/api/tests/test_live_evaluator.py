"""Production evaluator integration using local primary rules and short weather data."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agrix.agronomy import generate_candidates, load_agronomic_catalog
from agrix.errors import DomainError
from agrix.evaluator import evaluate, make_rotation
from agrix.models import (
    CropSelection,
    DailyWeather,
    EquipmentAccess,
    EvaluationRequest,
    Evidence,
    FarmConstraints,
    Plot,
    PlotProfile,
    Polygon,
    Scenario,
    SuggestionRequest,
)


def _plot(constraints=None):
    now = datetime.now(timezone.utc)
    return Plot(
        id="live-plot",
        project_id="live-project",
        name="Settat",
        revision=1,
        geometry=Polygon(
            coordinates=[
                [(-7.601, 33), (-7.600, 33), (-7.600, 33.001), (-7.601, 33.001), (-7.601, 33)]
            ]
        ),
        area_ha=Decimal(2),
        constraints=constraints or FarmConstraints(),
        created_at=now,
        updated_at=now,
        analysis_mode="live",
    )


def _profile(zone="settat"):
    return PlotProfile(
        plot_id="live-plot",
        plot_revision=1,
        fixture_version="local-import-test",
        zone_id=zone,
        rule_coverage="partial",
        metrics={},
        layers=[],
        synthetic=False,
        evidence=[
            Evidence(
                id="weather-test",
                source_id="local-weather",
                source_version="1",
                origin="modeled",
                synthetic=False,
            )
        ],
        weather=[
            DailyWeather(
                date=date(2024, 11, 1) + timedelta(days=i),
                rain_mm=i,
                tmin_c=10,
                tmax_c=20,
                evidence_ids=["weather-test"],
            )
            for i in range(5)
        ],
    )


def _request(sequence=None, **updates):
    fields = dict(
        plot_id="live-plot",
        plot_revision=1,
        start_date=date(2026, 11, 1),
        horizon_days=730,
        sequence=sequence or [CropSelection(crop_id="wheat")],
        scenario=Scenario(historical_years=[2024]),
    )
    fields.update(updates)
    return EvaluationRequest(**fields)


def test_live_evaluation_keeps_gaps_provenance_and_historical_inputs_separate():
    result = evaluate(
        _plot(), _profile(), _request(), load_agronomic_catalog(), analysis_id="analysis-test"
    )
    assert not result.synthetic
    assert result.status == "needs_confirmation"
    assert result.financials.whole_plot.expected_revenue.value is None
    assert result.financials.whole_plot.operating_cost.value is None
    assert result.market_history.data is None
    assert result.suppliers.data is None
    assert all(not operation.synthetic for operation in result.timeline.data)
    assert all(not item.synthetic for item in result.evidence)
    assert any(check.reason.code == "CROP_DURATION_CONFIRMATION" for check in result.checks)
    replay = result.climate_scenarios[0]
    assert replay["anchor_year"] == 2024
    assert replay["origin"] == "modeled"
    assert replay["rotation"]["coverage"]["rain_days"] == 5
    assert replay["rotation"]["rain_mm"] is None
    assert replay["yield_response"] is None
    assert result.input_snapshot.profile.weather[0].date == date(2024, 11, 1)
    assert result.comparison_period.end == date(2028, 10, 30)
    assert result.labor.availability.value == "partial"
    assert result.labor.data.paid_person_days is None
    assert result.labor.data.family_person_days is None
    assert result.labor.data.farm_person_days_by_month["2026-11"] is None


def test_chickpea_omitted_dates_use_region_specific_cross_year_calendar():
    catalog = load_agronomic_catalog()
    request = _request([CropSelection(crop_id="chickpea")])
    rotation = make_rotation(request, catalog, "saiss")
    assert rotation.start_date == date(2026, 12, 1)
    result = evaluate(_plot(), _profile("saiss"), request, catalog, analysis_id="analysis-test")
    assert result.rotation.start_date == date(2026, 12, 1)
    assert not any(check.outcome == "fail" for check in result.checks)


def test_suggestion_can_be_replayed_through_exact_same_evaluation_path():
    catalog = load_agronomic_catalog()
    request = SuggestionRequest(
        plot_id="live-plot",
        plot_revision=1,
        start_date=date(2026, 11, 1),
        horizon_days=730,
        scenario=Scenario(historical_years=[2024]),
    )
    candidates = generate_candidates(request, catalog, _profile())
    assert candidates
    first = evaluate(_plot(), _profile(), candidates[0], catalog, analysis_id="analysis-test")
    replay = evaluate(
        _plot(), _profile(), first.input_snapshot.request, catalog, analysis_id="analysis-test"
    )
    assert replay.fingerprint == first.fingerprint
    assert replay.rotation == first.rotation
    assert replay.financials == first.financials
    assert replay.climate_scenarios == first.climate_scenarios


def test_farmer_harvest_date_controls_harvest_operation():
    request = _request(
        [CropSelection(crop_id="wheat", start_date=date(2026, 11, 1), end_date=date(2027, 3, 1))]
    )
    result = evaluate(
        _plot(), _profile(), request, load_agronomic_catalog(), analysis_id="analysis-test"
    )
    harvest = next(operation for operation in result.timeline.data if operation.code == "harvest")
    assert harvest.date == date(2027, 3, 1)
    assert all(
        operation.date <= result.rotation.crops[0].end_date for operation in result.timeline.data
    )


def test_short_crop_calendar_returns_confirmation_error_without_dropping_operations():
    request = _request(
        [CropSelection(crop_id="wheat", start_date=date(2026, 11, 1), end_date=date(2026, 11, 20))]
    )
    with pytest.raises(DomainError) as error:
        evaluate(
            _plot(), _profile(), request, load_agronomic_catalog(), analysis_id="analysis-test"
        )
    assert error.value.code == "OPERATION_TIMING_REQUIRES_CONFIRMATION"


def test_manual_delayed_plan_uses_same_historical_anchor_as_planning_request():
    request = _request([CropSelection(crop_id="wheat", start_date=date(2027, 11, 1))])
    profile = _profile()
    profile.weather = [
        DailyWeather(
            date=date(2025, 11, 1), rain_mm=7, tmin_c=10, tmax_c=20, evidence_ids=["weather-test"]
        )
    ]
    result = evaluate(
        _plot(), profile, request, load_agronomic_catalog(), analysis_id="analysis-test"
    )
    assert result.climate_scenarios[0]["rotation"]["start_date"] == "2025-11-01"
    assert result.climate_scenarios[0]["rotation"]["coverage"]["rain_days"] == 1


def test_unavailable_machine_yields_failed_check_instead_of_uncaught_validation():
    constraints = FarmConstraints(equipment={"seed_drill": EquipmentAccess(mode="unavailable")})
    result = evaluate(
        _plot(constraints),
        _profile(),
        _request(),
        load_agronomic_catalog(),
        analysis_id="analysis-test",
    )
    assert result.status == "doesnt_fit"
    assert any(
        check.outcome == "fail" and check.reason.code == "MACHINERY_ACCESS_CHECK"
        for check in result.checks
    )


def test_unknown_region_chickpea_needs_explicit_dates_with_domain_error():
    with pytest.raises(DomainError):
        make_rotation(_request([CropSelection(crop_id="chickpea")]), load_agronomic_catalog(), None)


def test_published_calendar_conflict_is_failed_and_all_evidence_ids_resolve():
    request = _request([CropSelection(crop_id="wheat", start_date=date(2026, 12, 16))])
    result = evaluate(
        _plot(), _profile(), request, load_agronomic_catalog(), analysis_id="analysis-test"
    )
    assert result.status == "doesnt_fit"
    assert any(
        check.outcome == "fail" and check.reason.code == "SOWING_WINDOW_MISMATCH"
        for check in result.checks
    )
    known = {evidence.id for evidence in result.evidence}
    referenced = {eid for check in result.checks for eid in check.reason.evidence_ids}
    referenced.update(eid for operation in result.timeline.data for eid in operation.evidence_ids)
    referenced.update(eid for event in result.cash_events for eid in event.evidence_ids)
    assert referenced <= known


def test_explicit_fallow_does_not_create_a_sale_or_agronomic_promise():
    request = _request(
        [CropSelection(crop_id="fallow", start_date=date(2026, 11, 1), end_date=date(2027, 3, 1))],
        constraints=FarmConstraints(setup_cost_mad=0),
    )
    result = evaluate(
        _plot(), _profile(), request, load_agronomic_catalog(), analysis_id="analysis-test"
    )
    assert result.timeline.data == []
    assert all(event.kind != "sale" for event in result.cash_events)
    assert result.financials.whole_plot.expected_revenue.value == Decimal(0)
    assert result.financials.first_receipt_date is None
    assert any(
        check.outcome == "unknown" and check.reason.code == "FALLOW_MANAGEMENT_CONFIRMATION"
        for check in result.checks
    )


def test_labor_capacity_and_monthly_capacity_stay_unknown_when_demand_is_unknown():
    constraints = FarmConstraints(
        labor_person_days_available=1000,
        labor_by_month={"2026-11": 1000, "2026-12": 1000, "2027-04": 1000},
    )
    result = evaluate(
        _plot(constraints),
        _profile(),
        _request(),
        load_agronomic_catalog(),
        analysis_id="analysis-test",
    )
    capacity_checks = [
        check
        for check in result.checks
        if check.reason.code in ("LABOR_CAPACITY_CHECK", "MONTHLY_LABOR_CHECK")
    ]
    assert capacity_checks
    assert all(check.outcome == "unknown" for check in capacity_checks)
    assert all(check.reason.values["required_person_days"] is None for check in capacity_checks)
