from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from agrix.agronomy import (
    climate_exposure,
    generate_candidates,
    load_agronomic_catalog,
    maturity_dates,
    reference_evidence,
    resolve_rule_coverage,
    rule_checks,
)
from agrix.agronomy.planning import in_sowing_window
from agrix.models import (
    Availability,
    CropInstance,
    CropSelection,
    DailyWeather,
    EvaluationRequest,
    FarmConstraints,
    Metric,
    Origin,
    Plot,
    PlotProfile,
    Polygon,
    Rotation,
    Scenario,
    SoilTest,
    SuggestionRequest,
)


def _plot(**updates):
    now = datetime.now(timezone.utc)
    plot = Plot(
        id="field",
        project_id="project",
        name="Settat",
        revision=1,
        geometry=Polygon(
            coordinates=[[(-7.60, 33), (-7.59, 33), (-7.59, 33.01), (-7.60, 33.01), (-7.60, 33)]]
        ),
        area_ha=10,
        constraints=FarmConstraints(),
        created_at=now,
        updated_at=now,
    )
    return plot.model_copy(update=updates)


def _profile(**updates):
    profile = PlotProfile(
        plot_id="field",
        plot_revision=1,
        fixture_version="real-test",
        zone_id="settat",
        rule_coverage=Availability.partial,
        metrics={},
        evidence=[],
        layers=[],
        synthetic=False,
    )
    return profile.model_copy(update=updates)


def _rotation(start, end, crop_id="wheat"):
    return Rotation(
        crops=[
            CropInstance(
                id="first",
                crop_id=crop_id,
                start_date=start,
                end_date=end,
                duration_days=(end - start).days + 1,
            )
        ],
        start_date=start,
        end_date=end,
        duration_days=(end - start).days + 1,
    )


def test_real_catalog_preserves_primary_sources_and_economic_gaps():
    catalog = load_agronomic_catalog()
    assert not catalog.synthetic
    assert {crop.id for crop in catalog.crops} == {
        "wheat",
        "durum_wheat",
        "barley",
        "faba",
        "lentil",
        "chickpea",
        "fallow",
    }
    assert all(crop.budget_yield_status == "missing" for crop in catalog.crops)
    assert all(not op.costs_known for crop in catalog.crops for op in crop.operations)
    assert all(not op.labor_known for crop in catalog.crops for op in crop.operations)
    refs = reference_evidence(catalog)
    assert len({ref.id for ref in refs}) == len(refs)
    assert all(not ref.synthetic for ref in refs)
    assert any(ref.source_url and "2017" in ref.source_version for ref in refs)
    assert (
        next(ref for ref in refs if ref.id == "morocco-planning-assumptions-v1").origin
        == Origin.assumption
    )
    assert next(crop for crop in catalog.crops if crop.fallow).operations == []


def test_geographic_coverage_uses_entire_polygon_and_marks_lookup_uncertainty():
    catalog = load_agronomic_catalog()
    assert resolve_rule_coverage(_plot(), catalog) == ("settat", Availability.partial)
    crossing = Polygon(
        coordinates=[[(-7.60, 33), (-7.4, 33), (-7.4, 33.01), (-7.60, 33.01), (-7.60, 33)]]
    )
    assert resolve_rule_coverage(_plot(geometry=crossing), catalog) == (None, Availability.partial)
    outside = Polygon(
        coordinates=[
            [(-6.91, 30.92), (-6.90, 30.92), (-6.90, 30.93), (-6.91, 30.93), (-6.91, 30.92)]
        ]
    )
    assert resolve_rule_coverage(_plot(geometry=outside), catalog) == (
        None,
        Availability.unsupported,
    )


def test_inclusive_real_windows_and_regional_cross_year_chickpea():
    crops = {crop.id: crop for crop in load_agronomic_catalog().crops}
    wheat, chickpea = crops["wheat"], crops["chickpea"]
    assert in_sowing_window(date(2026, 12, 15), wheat, "settat")
    assert not in_sowing_window(date(2026, 12, 16), wheat, "settat")
    assert in_sowing_window(date(2027, 1, 31), chickpea, "saiss")
    assert not in_sowing_window(date(2027, 1, 31), chickpea, "settat")
    first, last = maturity_dates(
        CropSelection(crop_id="chickpea"), date(2026, 11, 1), chickpea, "saiss"
    )
    assert first == date(2026, 12, 1)
    assert (last - first).days + 1 == chickpea.duration_days
    first, _ = maturity_dates(CropSelection(crop_id="wheat"), date(2026, 12, 16), wheat, "settat")
    assert first == date(2027, 11, 1)
    start = date(2026, 11, 3)
    custom_end = date(2027, 5, 20)
    assert maturity_dates(
        CropSelection(crop_id="wheat", start_date=start, end_date=custom_end),
        date(2026, 11, 1),
        wheat,
        "settat",
    ) == (start, custom_end)


def test_unsupported_rules_are_unknown_and_barley_exclusion_is_sourced():
    catalog = load_agronomic_catalog()
    request = EvaluationRequest(
        plot_id="field",
        plot_revision=1,
        start_date=date(2026, 11, 1),
        sequence=[CropSelection(crop_id="barley")],
        constraints=FarmConstraints(previous_crops=["wheat"]),
    )
    rotation = _rotation(date(2026, 11, 1), date(2027, 4, 9), "barley")
    checks = rule_checks(_plot(), _profile(), request, catalog, rotation)
    exclusion = next(c for c in checks if c.reason.code == "DOCUMENTED_PREDECESSOR_EXCLUSION")
    assert exclusion.outcome == "fail"
    assert exclusion.reason.evidence_ids == ["morocco-cereals-2017"]
    assert any(
        c.outcome == "unknown" and c.reason.code == "CROP_DURATION_CONFIRMATION" for c in checks
    )
    unsupported = rule_checks(_plot(), _profile(zone_id=None), request, catalog, rotation)
    assert all(c.outcome != "fail" for c in unsupported)
    assert any(c.reason.code == "CROP_RULE_COVERAGE_MISSING" for c in unsupported)


def test_modelled_ph_cannot_categorically_reject_legume():
    catalog = load_agronomic_catalog()
    request = EvaluationRequest(
        plot_id="field",
        plot_revision=1,
        start_date=date(2026, 11, 1),
        sequence=[CropSelection(crop_id="faba")],
    )
    profile = _profile(
        metrics={
            "soil_ph": Metric(
                value=5,
                unit="pH",
                availability="available",
                origin="modeled",
                evidence_ids=["soilgrid-test"],
            )
        }
    )
    checks = rule_checks(
        _plot(), profile, request, catalog, _rotation(date(2026, 11, 1), date(2027, 4, 29), "faba")
    )
    check = next(c for c in checks if c.reason.code == "SOIL_PH_CONFIRMATION")
    assert check.outcome == "unknown"
    assert "soilgrid-test" in check.reason.evidence_ids


def test_bounded_candidates_fit_real_calendars_horizon_and_full_rotation():
    catalog = load_agronomic_catalog()
    request = SuggestionRequest(
        plot_id="field",
        plot_revision=1,
        start_date=date(2026, 8, 1),
        horizon_days=3650,
        constraints=FarmConstraints(previous_crops=["wheat"]),
    )
    suggestions = generate_candidates(request, catalog, _profile())
    assert 1 <= len(suggestions) <= 6
    crops = {c.id: c for c in catalog.crops}
    horizon_end = request.start_date + timedelta(days=request.horizon_days - 1)
    for candidate in suggestions:
        assert 3 < len(candidate.sequence) <= 20
        previous_end = request.start_date - timedelta(days=1)
        assert any(s.crop_id == "fallow" for s in candidate.sequence)
        for selection in candidate.sequence:
            assert previous_end < selection.start_date <= selection.end_date <= horizon_end
            assert in_sowing_window(selection.start_date, crops[selection.crop_id], "settat")
            previous_end = selection.end_date
        first_crop = next(s for s in candidate.sequence if s.crop_id != "fallow")
        assert first_crop.crop_id != "barley"
    assert generate_candidates(request, catalog, _profile(zone_id=None)) == []


def test_climate_replay_handles_cross_year_missing_rain_and_leap_day():
    rotation = _rotation(date(2027, 12, 30), date(2028, 1, 3))
    profile = _profile(
        weather=[
            DailyWeather(
                date=date(2023, 12, 30) + timedelta(days=i),
                rain_mm=(None if i == 2 else Decimal(0)),
                tmax_c=36 if i == 3 else 20,
                tmin_c=-1 if i == 0 else 10,
                evidence_ids=["daily-test"],
            )
            for i in range(5)
        ]
    )
    result = climate_exposure(rotation, profile, Scenario(historical_years=[2023]))[0]
    assert result["rotation"]["start_date"] == "2023-12-30"
    assert result["rotation"]["end_date"] == "2024-01-03"
    assert result["rotation"]["rain_mm"] is None
    assert result["rotation"]["max_dry_spell_days"] is None
    assert result["rotation"]["max_confirmed_dry_spell_days"] == 2
    assert result["rotation"]["heat_days"] == 1
    assert result["rotation"]["frost_days"] == 1
    assert result["evidence_ids"] == ["daily-test"]
    leap = climate_exposure(
        _rotation(date(2028, 2, 29), date(2028, 3, 1)),
        _profile(),
        Scenario(historical_years=[2023]),
    )[0]
    assert leap["rotation"]["start_date"] == "2023-02-28"
    assert leap["rotation"]["expected_days"] == 2
    assert leap["availability"] == "missing"


def test_rain_onset_is_defined_and_not_invented_across_missing_days():
    rotation = _rotation(date(2026, 11, 1), date(2026, 11, 6))
    weather = [
        DailyWeather(date=date(2024, 11, 1) + timedelta(days=i), rain_mm=rain, tmin_c=10, tmax_c=20)
        for i, rain in enumerate([None, 0, 0, 10, 10, 0])
    ]
    result = climate_exposure(
        rotation, _profile(weather=weather), Scenario(historical_years=[2024])
    )[0]
    onset = result["rotation"]["rain_onset"]
    assert onset["date"] is None
    assert onset["availability"] == "partial"
    assert onset["first_observed_candidate_date"] == "2024-11-03"
    assert result["yield_response"] is None


def test_historical_replay_anchors_delayed_first_crop_to_common_planning_year():
    weather = [
        DailyWeather(date=date(2025, 1, 1) + timedelta(days=i), rain_mm=2, tmin_c=10, tmax_c=20)
        for i in range(3)
    ]
    rotation = _rotation(date(2027, 1, 1), date(2027, 1, 3))
    result = climate_exposure(
        rotation,
        _profile(weather=weather),
        Scenario(historical_years=[2024]),
        planning_start=date(2026, 11, 1),
    )[0]
    assert result["rotation"]["start_date"] == "2025-01-01"
    assert result["rotation"]["rain_mm"] == 6
    assert result["availability"] == "available"


def test_latest_ph_test_wins_independent_of_input_order_and_retains_scope():
    tests = [
        SoilTest(
            property="soil_ph",
            value=7,
            unit="pH",
            depth_top_cm=0,
            depth_bottom_cm=30,
            sample_date=date(2025, 9, 1),
            source_label="Latest laboratory report",
        ),
        SoilTest(
            property="soil_ph",
            value=5,
            unit="pH",
            depth_top_cm=0,
            depth_bottom_cm=30,
            sample_date=date(2024, 9, 1),
            source_label="Older report",
        ),
    ]
    request = EvaluationRequest(
        plot_id="field",
        plot_revision=1,
        start_date=date(2026, 11, 1),
        sequence=[CropSelection(crop_id="faba")],
        constraints=FarmConstraints(soil_tests=tests),
    )
    checks = rule_checks(
        _plot(),
        _profile(),
        request,
        load_agronomic_catalog(),
        _rotation(date(2026, 11, 1), date(2027, 4, 29), "faba"),
    )
    ph_check = next(check for check in checks if check.reason.code == "SOIL_PH_WITHIN_GUIDE")
    assert ph_check.outcome == "pass"
    assert ph_check.reason.values["sample_source"] == "Latest laboratory report"
    assert ph_check.reason.values["sample_date"] == "2025-09-01"
    assert ph_check.reason.values["depth_top_cm"] == 0
    assert ph_check.reason.values["depth_bottom_cm"] == 30
