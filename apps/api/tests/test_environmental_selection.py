"""Environmental counterfactuals: timing, ranking, water conservation and provenance."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agrix.agronomy import generate_candidates, load_agronomic_catalog
from agrix.agronomy.environment import EnvironmentalModel, hargreaves, penman_monteith
from agrix.evaluator import evaluate
from agrix.models import (
    Catalog,
    CropSelection,
    DailyWeather,
    DateRange,
    EvaluationRequest,
    Evidence,
    FarmConstraints,
    Metric,
    Plot,
    PlotProfile,
    Polygon,
    Scenario,
    SoilTest,
    SuggestionRequest,
)

START = date(2026, 11, 1)


def profile(rain=2, temperature=15, slope=0, moisture="0.20", texture=True):
    def metric(value, unit, **kwargs):
        return Metric(
            value=value,
            unit=unit,
            availability="available",
            origin="modeled",
            evidence_ids=["environment-test"],
            **kwargs,
        )

    metrics = {
        "soil_ph": metric(7, "pH"),
        "terrain_slope": metric(slope, "degrees"),
        "soil_moisture": metric(moisture, "m3/m3", depth_cm=(0, 100)),
        "elevation": metric(300, "m"),
        "ndvi": metric("0.4", "index"),
    }
    if texture:
        metrics.update(
            {
                key: metric(value, "%")
                for key, value in (("sand_pct", 40), ("clay_pct", 20), ("silt_pct", 40))
            }
        )
    rows = []
    for i in range(1096):
        day = date(2023, 1, 1) + timedelta(days=i)
        rows.append(
            DailyWeather(
                date=day,
                rain_mm=rain(day) if callable(rain) else rain,
                tmin_c=temperature - 7,
                tmax_c=temperature + 7,
                tmean_c=temperature,
                evidence_ids=["environment-test"],
            )
        )
    return PlotProfile(
        plot_id="field",
        plot_revision=1,
        fixture_version="environment-test-v1",
        zone_id="settat",
        rule_coverage="partial",
        metrics=metrics,
        layers=[],
        weather=rows,
        synthetic=False,
        evidence=[
            Evidence(
                id="environment-test",
                source_id="controlled-test-data",
                source_version="1",
                origin="modeled",
                synthetic=True,
                geographic_bounds=(-7.7, 32.9, -7.5, 33.1),
                period=DateRange(start=date(2023, 1, 1), end=date(2025, 12, 31)),
            )
        ],
    )


def plot(constraints=None):
    now = datetime.now(timezone.utc)
    return Plot(
        id="field",
        project_id="test",
        revision=1,
        name="Synthetic test field",
        geometry=Polygon(
            coordinates=[[(-7.6, 33), (-7.59, 33), (-7.59, 33.01), (-7.6, 33.01), (-7.6, 33)]]
        ),
        area_ha=2,
        constraints=constraints or FarmConstraints(),
        analysis_mode="live",
        created_at=now,
        updated_at=now,
    )


def selection(crop_id="wheat", start=START, days=180):
    return CropSelection(
        crop_id=crop_id, start_date=start, end_date=start + timedelta(days=days - 1)
    )


def component(result, key):
    return next(c for c in result["components"] if c["id"] == key)


def assess(data, selections=None, constraints=None):
    return EnvironmentalModel(data, constraints, START).assess(
        selections or [selection()], load_agronomic_catalog()
    )


def test_hargreaves_uses_correct_radiation_units_and_rejects_incomplete_temperatures():
    # FAO56 example 15: July 15, Lyon; Ra about 40.6 MJ/m²/day.
    assert hargreaves(date(2025, 7, 15), 45.72, 14.8, 26.6, 20.7) == pytest.approx(5.04, abs=0.04)
    assert hargreaves(date(2025, 7, 15), 45.72, None, 26.6, 20.7) is None
    assert hargreaves(date(2025, 7, 15), 45.72, 26.6, 14.8, 20.7) is None


def test_penman_monteith_uses_radiation_humidity_and_wind_when_supplied():
    # FAO56 daily example 18, Uccle July 6: about 3.9 mm/day.
    # Equivalent mean RH reconstructs the example's 1.409 kPa vapour pressure.
    assert penman_monteith(
        date(2025, 7, 6), 50.8, 12.3, 21.5, 16.9, 1.409 / 1.997 * 100, 2.078, 22.07, 100
    ) == pytest.approx(3.9, abs=0.06)
    data = profile()
    for row in data.weather:
        row.humidity_pct = Decimal(70)
        row.wind_m_s = Decimal(2)
        row.solar_mj_m2 = Decimal(15)
    model = EnvironmentalModel(data, None, START)
    assert model.et_methods == {"fao56_penman_monteith_mean_rh"}
    fallback = EnvironmentalModel(profile(), None, START)
    assert fallback.et_methods == {"hargreaves_temperature_fallback"}
    day = date(2024, 11, 1)
    original = model.et0[day]
    data.weather = [
        row.model_copy(update={"humidity_pct": Decimal(30), "wind_m_s": Decimal(5)})
        for row in data.weather
    ]
    assert EnvironmentalModel(data, None, START).et0[day] > original


def test_complete_cold_weather_reports_unreached_maturity_instead_of_short_nominal_cycle():
    data = profile(temperature=1)
    crop = load_agronomic_catalog().crops[0]
    result = EnvironmentalModel(data, None, START).estimate_duration(crop, START)
    assert result["maturity_unreached_in_complete_year"]
    assert result["method"] == "historical_thermal_target_unreached"
    assert result["duration_days"] == crop.environmental_parameters.cycle_days[1]
    assert result["latest_days"] is None


def test_temperature_changes_automatic_duration_and_explicit_dates_remain_authoritative():
    catalog = load_agronomic_catalog()
    wheat = next(c for c in catalog.crops if c.id == "wheat")
    cold = EnvironmentalModel(profile(temperature=10), None, START).estimate_duration(wheat, START)
    warm = EnvironmentalModel(profile(temperature=20), None, START).estimate_duration(wheat, START)
    assert warm["duration_days"] < cold["duration_days"]
    assert warm["method"] == cold["method"] == "historical_thermal_time"
    assert not warm["calibrated"]
    request = EvaluationRequest(
        plot_id="field", plot_revision=1, start_date=START, sequence=[selection(days=150)]
    )
    result = evaluate(plot(), profile(temperature=20), request, catalog, analysis_id="a")
    assert result.rotation.crops[0].duration_days == 150
    assert result.rotation.crops[0].timing["duration_days"] < 150
    assert result.rotation.crops[0].timing["explicit_end_preserved"]


def test_missing_thermal_days_do_not_accumulate_as_zero_or_receive_invented_maturity():
    data = profile()
    for row in data.weather:
        if row.date.month == 11 and row.date.day == 2:
            row.tmean_c = row.tmin_c = row.tmax_c = None
    wheat = load_agronomic_catalog().crops[0]
    result = EnvironmentalModel(data, None, START).estimate_duration(wheat, START)
    assert result["method"] == "nominal_fallback_missing_thermal_evidence"
    assert result["complete_maturity_years"] == 0
    assert result["latest_days"] is None
    assert result["range_censored"]


def test_rain_and_slope_change_water_risk_without_changing_assumed_revenue():
    dry, wet = profile(rain=0.1), profile(rain=6)
    dry_result, wet_result = assess(dry), assess(wet)
    assert component(wet_result, "water")["score"] > component(dry_result, "water")["score"]
    assert component(wet_result, "drought")["score"] > component(dry_result, "drought")["score"]
    flat, steep = assess(profile(rain=1)), assess(profile(rain=1, slope=25))
    assert component(steep, "water")["score"] < component(flat, "water")["score"]
    assert component(steep, "terrain")["score"] < component(flat, "terrain")["score"]
    constraints = FarmConstraints(
        yield_kg_per_ha={"wheat": Decimal(2000)}, sale_price_mad_per_kg={"wheat": Decimal(3)}
    )
    request = EvaluationRequest(
        plot_id="field",
        plot_revision=1,
        start_date=START,
        sequence=[selection()],
        constraints=constraints,
    )
    evaluations = [
        evaluate(plot(), data, request, load_agronomic_catalog(), analysis_id="a")
        for data in (dry, wet)
    ]
    assert evaluations[0].financials == evaluations[1].financials
    assert evaluations[0].environmental != evaluations[1].environmental


def test_root_zone_context_affects_score_but_surface_moisture_does_not_replace_it():
    low, high = profile(moisture="0.16"), profile(moisture="0.30")
    assert (
        component(assess(high), "soil_moisture")["score"]
        > component(assess(low), "soil_moisture")["score"]
    )
    low.metrics["soil_moisture"].depth_cm = (0, 5)
    assert component(assess(low), "soil_moisture")["score"] is None
    model = EnvironmentalModel(high, None, START)
    assert model._theta(date(2024, 11, 1)) is None  # A multi-year mean cannot initialize any day.
    high.evidence[0].period = DateRange(start=date(2024, 11, 1), end=date(2024, 11, 1))
    model = EnvironmentalModel(high, None, START)
    assert model._theta(date(2024, 11, 1)) == pytest.approx(0.30)
    assert model._theta(date(2025, 11, 1)) is None


def test_unknown_soil_never_produces_a_fabricated_root_zone_reservoir():
    data = profile(texture=False)
    result = assess(data)
    water = next(c for c in result["crops"][0]["components"] if c["id"] == "water")
    assert water["values"]["method"] == "rainfall_demand_screen_no_soil_storage"
    assert component(result, "soil_texture")["score"] is None
    assert component(result, "soil_moisture")["score"] is None


def test_missing_rain_is_not_counted_as_a_dry_spell_or_full_water_coverage():
    data = profile(rain=None)
    result = assess(data)
    assert component(result, "water")["score"] is None
    assert component(result, "drought")["score"] is None
    assert result["score_range"][1] > result["score_range"][0]
    assert result["coverage"] < assess(profile())["coverage"]


def test_sequence_water_storage_is_carried_and_fallow_rain_recharges_it():
    catalog = load_agronomic_catalog()
    data = profile(rain=0)
    initial_day = date(2024, 11, 1)
    data.evidence.append(
        Evidence(
            id="initial-water",
            source_id="test-measurement",
            source_version="1",
            origin="observed",
            period=DateRange(start=initial_day, end=initial_day),
        )
    )
    data.metrics["soil_moisture"].value = Decimal("0.32")
    data.metrics["soil_moisture"].evidence_ids = ["initial-water"]
    first = selection(days=50)
    second = selection("faba", start=START + timedelta(days=60), days=50)
    idle = selection("fallow", start=first.end_date + timedelta(days=1), days=10)
    model = EnvironmentalModel(data, None, START, Scenario(historical_years=[2024]))
    dry = model._water([first, idle, second], {c.id: c for c in catalog.crops})
    for row in data.weather:
        if date(2024, 12, 21) <= row.date <= date(2024, 12, 30):
            row.rain_mm = Decimal(40)
    wet = EnvironmentalModel(data, None, START, Scenario(historical_years=[2024]))._water(
        [first, idle, second], {c.id: c for c in catalog.crops}
    )
    assert wet[2]["actual_low"] > dry[2]["actual_low"]
    assert 0 <= wet[2]["actual_low"] <= wet[2]["demand"]


def test_water_balance_counts_actual_historical_leap_days_once():
    catalog = load_agronomic_catalog()
    model = EnvironmentalModel(profile(rain=2), None, START, Scenario(historical_years=[2023]))
    selected = selection(days=180)
    stats = model._water([selected], {c.id: c for c in catalog.crops})[0]
    expected = (date(2024, 4, 29) - date(2023, 11, 1)).days + 1
    assert stats["expected"] == stats["known"] == expected
    assert stats["rain"] == 2 * expected


def test_farmer_soil_override_changes_environment_and_all_evidence_resolves():
    constraints = FarmConstraints(
        soil_tests=[
            SoilTest(
                property="soil_ph",
                value=5,
                unit="pH",
                depth_top_cm=0,
                depth_bottom_cm=20,
                sample_date=date(2025, 1, 1),
                source_label="Synthetic lab record for regression",
            )
        ]
    )
    request = EvaluationRequest(
        plot_id="field",
        plot_revision=1,
        start_date=START,
        sequence=[selection("faba")],
        constraints=constraints,
    )
    result = evaluate(plot(), profile(), request, load_agronomic_catalog(), analysis_id="a")
    assert result.status == "doesnt_fit"
    assert component(result.environmental, "soil_ph")["score"] < 100
    assert set(result.environmental["evidence_ids"]) <= {e.id for e in result.evidence}


def test_environmental_suggestions_replay_exactly_and_ignore_economic_overrides():
    data, catalog = profile(), load_agronomic_catalog()
    request = SuggestionRequest(
        plot_id="field",
        plot_revision=1,
        start_date=START,
        horizon_days=365,
        constraints=FarmConstraints(previous_crops=["faba"]),
    )
    suggestions = generate_candidates(request, catalog, data, plot=plot())
    assert suggestions
    original = evaluate(plot(), data, suggestions[0], catalog, analysis_id="a")
    replay = evaluate(plot(), data, original.input_snapshot.request, catalog, analysis_id="a")
    assert original.environmental == replay.environmental
    assert original.fingerprint == replay.fingerprint
    request.constraints.yield_kg_per_ha = {"wheat": Decimal(9999)}
    request.constraints.sale_price_mad_per_kg = {"wheat": Decimal(99)}
    expensive = generate_candidates(request, catalog, data, plot=plot())
    assert [c.sequence for c in suggestions] == [c.sequence for c in expensive]


def test_old_catalog_snapshots_do_not_acquire_new_environmental_parameters():
    raw = load_agronomic_catalog().model_dump(mode="json")
    for crop in raw["crops"]:
        crop.pop("environmental_parameters")
    old = Catalog.model_validate(raw)
    assert all(c.environmental_parameters is None for c in old.crops)


def test_sowing_date_search_responds_to_seasonal_rain_timing():
    catalog = load_agronomic_catalog()
    # Restrict this counterfactual to the same crop so a changed date cannot be attributed to crop IDs.
    catalog.crops = [c for c in catalog.crops if c.id in ("barley", "fallow")]
    request = SuggestionRequest(
        plot_id="field",
        plot_revision=1,
        start_date=START,
        horizon_days=365,
        constraints=FarmConstraints(previous_crops=["faba"]),
    )
    dates = []
    for rainy_months in ((11,), (12, 1)):
        data = profile(rain=lambda day, months=rainy_months: 6 if day.month in months else 0)
        candidates = generate_candidates(request, catalog, data, plot=plot())
        dates.append(next(s.start_date for s in candidates[0].sequence if s.crop_id == "barley"))
    assert dates[0] != dates[1]


def test_suggested_rotations_have_distinct_crops_not_only_different_dates():
    data, catalog = profile(), load_agronomic_catalog()
    request = SuggestionRequest(
        plot_id="field",
        plot_revision=1,
        start_date=START,
        horizon_days=730,
        constraints=FarmConstraints(),
    )
    suggestions = generate_candidates(request, catalog, data, plot=plot())
    paths = [
        tuple(s.crop_id for s in item.sequence if s.crop_id != "fallow") for item in suggestions
    ]
    assert len(paths) >= 3
    assert len(paths) == len(set(paths))
    assert len({tuple(sorted(path)) for path in paths[:3]}) == 3
