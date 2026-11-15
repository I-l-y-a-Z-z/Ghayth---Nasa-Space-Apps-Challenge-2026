from datetime import date, timedelta
from decimal import Decimal

import pytest

from agrix.finance import build_budget
from agrix.models import (
    Availability,
    Catalog,
    CropInstance,
    CropProfile,
    FarmConstraints,
    OperationTemplate,
    Rotation,
    Scenario,
)

D = Decimal
ONE = D(1)


@pytest.fixture
def catalog():
    return Catalog(
        version="finance-test-v1",
        fixture_version="finance-test-v1",
        crops=[
            CropProfile(
                id="wheat",
                label_key="crop.wheat",
                group="cereal",
                zone_ids=["test-zone"],
                production_systems=["rainfed"],
                sowing_months=[1],
                duration_days=10,
                yield_kg_per_ha=D("2000"),
                sale_price_mad_per_kg=D("3"),
                operations=[
                    OperationTemplate(
                        id="sow",
                        offset_days=0,
                        inputs_mad_per_ha=D("800"),
                        machinery_mad_per_ha=D("1000"),
                        machinery_id="tractor",
                        machinery_mode="hired_with_operator",
                        paid_person_days_per_ha=D("2"),
                        family_person_days_per_ha=D("3"),
                        included_operator_person_days_per_ha=D("1"),
                    )
                ],
            )
        ],
    )


def rotation(*starts):
    starts = starts or (date(2025, 1, 1),)
    instances = [
        CropInstance(
            id=f"crop-{index}",
            crop_id="wheat",
            start_date=start,
            end_date=start + timedelta(days=9),
            duration_days=10,
        )
        for index, start in enumerate(starts)
    ]
    return Rotation(
        crops=instances,
        start_date=min(starts),
        end_date=max(crop.end_date for crop in instances),
        duration_days=(max(crop.end_date for crop in instances) - min(starts)).days + 1,
    )


def budget(catalog, *, constraints=None, area=ONE, allow=True, plan=None, scenario=None):
    return build_budget(
        plan or rotation(),
        catalog,
        constraints or FarmConstraints(),
        area,
        scenario or Scenario(),
        allow,
    )


def test_independently_checked_one_hectare_budget(catalog):
    operations, events, result, labor, machinery = budget(catalog)
    totals = result.whole_plot
    assert totals.initial_investment.value == D("200.00")
    assert totals.operating_cost.value == D("2000.00")
    assert totals.expected_revenue.value == D("6000.00")
    assert totals.operating_margin.value == D("4000.00")
    assert totals.net_cash_flow.value == D("3800.00")
    assert totals.cash_before_first_receipt.value == D("2200.00")
    assert totals.peak_funding_need.value == D("2200.00")
    assert result.first_receipt_date == date(2025, 2, 9)
    assert result.last_cash_event_date == date(2025, 2, 9)
    assert result.break_even_yield.availability == Availability.available
    assert result.break_even_yield.value == D("666.666667")
    assert result.break_even_price.value == D("1.00")
    assert result.per_ha == result.whole_plot.model_copy(
        update={
            name: getattr(totals, name).model_copy(update={"unit": "MAD/ha"})
            for name in totals.__class__.model_fields
        }
    )
    assert sum(event.kind == "setup" for event in events) == 1
    assert operations[0].total_cash_cost_mad == D("2000.00")
    # Family work is unpaid; contractor operator time is already in machinery.
    assert labor.paid_cost_mad == D("200.00")
    assert labor.paid_person_days == D(2)
    assert labor.family_person_days == D(3)
    assert labor.included_operator_person_days == D(1)
    assert labor.farm_person_days_by_month == {"2025-01": D(5)}
    assert machinery[0].operator_included is True
    assert machinery[0].cost_mad == D("1000.00")


def test_late_payment_increases_two_crop_peak_and_setup_is_once(catalog):
    plan = rotation(date(2025, 1, 1), date(2025, 2, 1))
    _, late_events, late, _, _ = budget(
        catalog, plan=plan, constraints=FarmConstraints(payment_lag_days=60)
    )
    _, _, immediate, _, _ = budget(
        catalog, plan=plan, constraints=FarmConstraints(payment_lag_days=0)
    )
    assert late.whole_plot.peak_funding_need.value == D("4200.00")
    assert late.whole_plot.cash_before_first_receipt.value == D("4200.00")
    assert immediate.whole_plot.peak_funding_need.value == D("2200.00")
    assert late.whole_plot.net_cash_flow.value == immediate.whole_plot.net_cash_flow.value
    assert sum(event.kind == "setup" for event in late_events) == 1


def test_unknown_yield_or_price_preserves_costs_but_not_funding(catalog):
    _, events, result, _, _ = budget(catalog, allow=False)
    assert result.whole_plot.operating_cost.value == D("2000.00")
    for field in (
        "expected_revenue",
        "operating_margin",
        "net_cash_flow",
        "cash_before_first_receipt",
        "peak_funding_need",
    ):
        metric = getattr(result.whole_plot, field)
        assert metric.value is None
        assert metric.availability == Availability.missing
        assert metric.reason.code == "REVENUE_EVIDENCE_MISSING"
        assert getattr(result.per_ha, field).value is None
    sale = next(event for event in events if event.kind == "sale")
    assert sale.amount_mad is None
    assert sale.reason.code == "YIELD_OR_PRICE_MISSING"


def test_both_farmer_overrides_work_without_regional_catalog_assumptions(catalog):
    constraints = FarmConstraints(
        yield_kg_per_ha={"wheat": D("1000")},
        sale_price_mad_per_kg={"wheat": D("4")},
    )
    _, _, result, _, _ = budget(catalog, constraints=constraints, allow=False)
    assert result.whole_plot.expected_revenue.value == D("4000.00")
    only_price = constraints.model_copy(update={"yield_kg_per_ha": {}})
    _, _, unknown, _, _ = budget(catalog, constraints=only_price, allow=False)
    assert unknown.whole_plot.expected_revenue.value is None


def test_zero_is_a_real_value_not_a_missing_override(catalog):
    constraints = FarmConstraints(
        yield_kg_per_ha={"wheat": D(0)},
        sale_price_mad_per_kg={"wheat": D(0)},
        setup_cost_mad=D(0),
        wage_mad_per_person_day=D(0),
    )
    _, events, result, labor, _ = budget(catalog, constraints=constraints, allow=False)
    sale = next(event for event in events if event.kind == "sale")
    assert sale.amount_mad == D("0.00")
    assert sale.availability == Availability.available
    assert result.whole_plot.expected_revenue.value == D("0.00")
    assert result.whole_plot.operating_margin.value == D("-1800.00")
    assert result.whole_plot.peak_funding_need.value == D("1800.00")
    assert labor.paid_cost_mad == D("0.00")


def test_area_and_scenario_scale_outputs(catalog):
    _, _, result, labor, _ = budget(
        catalog,
        area=D("2.5"),
        scenario=Scenario(yield_multiplier=D("0.5"), price_multiplier=D("0.8")),
    )
    assert result.whole_plot.operating_cost.value == D("5000.00")
    assert result.whole_plot.expected_revenue.value == D("6000.00")
    assert result.per_ha.initial_investment.value == D("80.00")
    assert result.per_ha.operating_cost.value == D("2000.00")
    assert result.per_ha.net_cash_flow.value == D("320.00")
    assert labor.farm_person_days_by_month == {"2025-01": D("12.5")}


def test_same_date_cash_is_netted_and_pre_receipt_excludes_that_date(catalog):
    one_day = rotation()
    one_day.crops[0].end_date = one_day.crops[0].start_date
    _, _, result, _, _ = budget(
        catalog, plan=one_day, constraints=FarmConstraints(payment_lag_days=0)
    )
    assert result.whole_plot.peak_funding_need.value == D("0.00")
    assert result.whole_plot.cash_before_first_receipt.value == D("0.00")


def test_operations_dates_booking_and_quantity_precision(catalog):
    catalog.crops[0].operations[0].offset_days = 3
    ops, _, result, labor, _ = budget(catalog, area=D("0.123456"))
    assert ops[0].date == date(2025, 1, 4)
    assert ops[0].booking_window.start == date(2024, 12, 21)
    assert ops[0].booking_window.end == date(2025, 1, 4)
    assert ops[0].paid_person_days == D("0.246912")
    assert labor.paid_cost_mad == D("24.69")
    # Each component is rounded to cents: 98.76 inputs + 123.46 machine + 24.69 wages.
    assert result.whole_plot.operating_cost.value == D("246.91")


@pytest.mark.parametrize("area", [D(0), D(-1), D("NaN"), D("Infinity"), True])
def test_invalid_area_rejected(catalog, area):
    with pytest.raises(ValueError, match="area_ha"):
        budget(catalog, area=area)


def test_computed_money_limit_is_enforced(catalog):
    with pytest.raises(ValueError, match="Money must"):
        budget(catalog, area=D("999999999999"))


def test_payment_date_calendar_overflow_is_explicit(catalog):
    plan = rotation(date(9999, 12, 1))
    with pytest.raises(ValueError, match="Payment date"):
        budget(catalog, plan=plan, constraints=FarmConstraints(payment_lag_days=30))
