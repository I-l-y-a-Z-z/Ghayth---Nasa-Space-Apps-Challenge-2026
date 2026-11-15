"""Independent arithmetic and missing-input checks for the production budget path."""

from datetime import date
from decimal import Decimal

import pytest

from agrix.finance import build_budget
from agrix.models import (
    AdditionalCost,
    Catalog,
    CropInstance,
    CropProfile,
    EquipmentAccess,
    FarmConstraints,
    OperationCostOverride,
    OperationTemplate,
    Rotation,
    Scenario,
)

D = Decimal
TWO = D(2)


def _catalog(*, quantities=None, machine=True):
    return Catalog(
        version="production-finance-test",
        fixture_version="production-finance-test",
        synthetic=False,
        crops=[
            CropProfile(
                id="wheat",
                label_key="crop.wheat",
                group="cereal",
                zone_ids=["settat"],
                production_systems=["rainfed"],
                sowing_months=[11],
                duration_days=10,
                yield_kg_per_ha=0,
                sale_price_mad_per_kg=0,
                budget_yield_status="missing",
                synthetic=False,
                operations=[
                    OperationTemplate(
                        id="work",
                        offset_days=0,
                        costs_known=False,
                        machinery_id="tractor" if machine else None,
                        machinery_mode="hired_with_operator" if machine else "none",
                        paid_person_days_per_ha=2,
                        family_person_days_per_ha=3,
                        included_operator_person_days_per_ha=1 if machine else 0,
                        quantities=quantities or [],
                    )
                ],
            )
        ],
    )


def _rotation():
    return Rotation(
        crops=[
            CropInstance(
                id="season-1",
                crop_id="wheat",
                start_date=date(2026, 11, 1),
                end_date=date(2026, 11, 10),
                duration_days=10,
            )
        ],
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 10),
        duration_days=10,
    )


def _constraints(**updates):
    fields = dict(
        yield_kg_per_ha={"wheat": D(1000)},
        sale_price_mad_per_kg={"wheat": D(4)},
        setup_cost_mad=D(200),
        wage_mad_per_person_day=D(100),
        operation_cost_overrides={
            "wheat:work": OperationCostOverride(inputs_mad_per_ha=800, machinery_mad_per_ha=1000)
        },
    )
    fields.update(updates)
    return FarmConstraints(**fields)


def _budget(catalog=None, constraints=None, scenario=None, area=TWO):
    return build_budget(
        _rotation(),
        catalog or _catalog(),
        constraints or _constraints(),
        area,
        scenario or Scenario(),
        False,
    )


def test_production_missing_quotes_and_zero_catalog_placeholders_remain_unknown():
    operations, events, financials, _, _ = _budget(constraints=FarmConstraints(setup_cost_mad=None))
    totals = financials.whole_plot
    assert totals.expected_revenue.value is None
    assert totals.operating_cost.value is None
    assert totals.initial_investment.value is None
    assert totals.peak_funding_need.value is None
    assert totals.net_cash_flow.value is None
    assert operations[0].total_cash_cost_mad is None
    assert next(event for event in events if event.kind == "sale").amount_mad is None


def test_explicit_local_quotes_additional_costs_and_solo_crop_break_even():
    constraints = _constraints(
        additional_costs=[
            AdditionalCost(
                id="lease",
                label="Land lease",
                date=date(2026, 10, 25),
                kind="setup",
                amount_mad=1000,
            ),
            AdditionalCost(
                id="transport",
                label="Transport",
                date=date(2026, 11, 12),
                kind="operating",
                amount_mad=300,
            ),
        ]
    )
    operations, events, financials, labor, machines = _budget(constraints=constraints)
    totals = financials.whole_plot
    # 2 ha x (800 inputs + 1000 contractor + 2 paid days x 100) + 300 transport.
    assert totals.operating_cost.value == D("4300.00")
    assert totals.initial_investment.value == D("1200.00")
    assert totals.expected_revenue.value == D("8000.00")
    assert totals.operating_margin.value == D("3700.00")
    assert totals.net_cash_flow.value == D("2500.00")
    assert totals.peak_funding_need.value == D("5500.00")
    assert financials.per_ha.operating_cost.value == D("2150.00")
    assert financials.break_even_yield.value == D("537.500000")
    assert financials.break_even_price.value == D("2.15")
    assert len([event for event in events if event.id == "cash:setup"]) == 1
    assert labor.included_operator_person_days == D(2)
    assert labor.paid_person_days == D(4)
    assert operations[0].paid_labor_cost_mad == D("400.00")
    assert machines[0].operator_included is True


@pytest.mark.parametrize("mode", ["owned", "shared"])
@pytest.mark.parametrize(
    "operator_labor,expected_paid,expected_family,expected_operating",
    [
        ("paid", 6, 6, "2200.00"),
        ("family", 4, 8, "2000.00"),
    ],
)
def test_owned_shared_equipment_has_no_rental_fee_and_operator_counted_once(
    mode,
    operator_labor,
    expected_paid,
    expected_family,
    expected_operating,
):
    equipment = {"tractor": EquipmentAccess(mode=mode, operator_labor=operator_labor)}
    operations, _, financials, labor, _ = _budget(constraints=_constraints(equipment=equipment))
    assert operations[0].machinery_cost_mad == D(0)
    assert financials.whole_plot.operating_cost.value == D(expected_operating)
    assert labor.paid_person_days == D(expected_paid)
    assert labor.family_person_days == D(expected_family)
    assert labor.included_operator_person_days == D(0)


@pytest.mark.parametrize("mode", ["unknown", "unavailable"])
def test_unknown_or_unavailable_machine_is_an_explicit_gap_not_model_exception(mode):
    operations, _, financials, _, _ = _budget(
        constraints=_constraints(equipment={"tractor": EquipmentAccess(mode=mode)})
    )
    assert operations[0].machinery_cost_mad is None
    assert financials.whole_plot.operating_cost.value is None


def test_unknown_material_quantity_stays_null():
    catalog = _catalog(
        quantities=[{"label": "soil-test-dependent input", "unit": "kg/ha", "per_ha": None}]
    )
    operations, _, _, _, _ = _budget(catalog=catalog)
    assert operations[0].quantities[0]["quantity"] is None


def test_input_only_override_does_not_require_quote_for_non_machine_operation():
    constraints = _constraints(
        operation_cost_overrides={"wheat:work": OperationCostOverride(inputs_mad_per_ha=800)}
    )
    operations, _, financials, _, _ = _budget(
        catalog=_catalog(machine=False), constraints=constraints
    )
    assert operations[0].machinery_cost_mad == D(0)
    assert financials.whole_plot.operating_cost.value == D("2000.00")


def test_subcent_effective_price_has_undefined_break_even_not_division_error():
    _, _, financials, _, _ = _budget(
        constraints=_constraints(sale_price_mad_per_kg={"wheat": D("0.01")}),
        scenario=Scenario(price_multiplier=D("0.1")),
    )
    assert financials.whole_plot.expected_revenue.value == D(0)
    assert financials.break_even_yield.value is None
    assert financials.break_even_yield.reason.code == "BREAK_EVEN_PRICE_NONPOSITIVE"


def test_null_wage_preserves_unknown_paid_labor_cost():
    operations, _, financials, labor, _ = _budget(
        constraints=_constraints(wage_mad_per_person_day=None)
    )
    assert operations[0].paid_labor_cost_mad is None
    assert operations[0].total_cash_cost_mad is None
    assert labor.paid_cost_mad is None
    assert labor.paid_person_days == D(4)
    assert labor.family_person_days == D(6)
    assert labor.included_operator_person_days == D(2)
    assert labor.farm_person_days_by_month == {"2026-11": D(10)}
    assert financials.whole_plot.operating_cost.value is None


@pytest.mark.parametrize(
    "labor_overrides",
    [
        {},
        {"paid_person_days_per_ha": 1},
        {"paid_person_days_per_ha": 1, "family_person_days_per_ha": 2},
    ],
)
def test_unknown_labor_remains_unknown_despite_complete_material_and_machine_quotes(
    labor_overrides,
):
    catalog = _catalog()
    catalog.crops[0].operations[0].labor_known = False
    constraints = _constraints(
        operation_cost_overrides={
            "wheat:work": OperationCostOverride(
                inputs_mad_per_ha=800, machinery_mad_per_ha=1000, **labor_overrides
            )
        }
    )
    operations, events, financials, labor, _ = _budget(catalog=catalog, constraints=constraints)
    assert operations[0].inputs_cost_mad == D(1600)
    assert operations[0].machinery_cost_mad == D(2000)
    assert operations[0].paid_labor_cost_mad is None
    assert operations[0].total_cash_cost_mad is None
    assert operations[0].paid_person_days is None
    assert operations[0].family_person_days is None
    assert operations[0].included_operator_person_days is None
    assert labor.paid_cost_mad is None
    assert labor.paid_person_days is None
    assert labor.family_person_days is None
    assert labor.included_operator_person_days is None
    assert labor.farm_person_days_by_month == {"2026-11": None}
    assert financials.whole_plot.operating_margin.value is None
    assert financials.whole_plot.peak_funding_need.value is None
    event = next(event for event in events if event.kind == "operating")
    assert event.reason.values["labor_missing"] is True


def test_all_three_local_labor_demands_establish_known_labor_cost():
    catalog = _catalog()
    catalog.crops[0].operations[0].labor_known = False
    constraints = _constraints(
        operation_cost_overrides={
            "wheat:work": OperationCostOverride(
                inputs_mad_per_ha=800,
                machinery_mad_per_ha=1000,
                paid_person_days_per_ha=1,
                family_person_days_per_ha=2,
                operator_days_per_ha=3,
            )
        }
    )
    operations, _, financials, labor, _ = _budget(catalog=catalog, constraints=constraints)
    assert operations[0].paid_labor_cost_mad == D(200)
    assert labor.paid_person_days == D(2)
    assert labor.family_person_days == D(4)
    assert labor.included_operator_person_days == D(6)
    assert financials.whole_plot.operating_cost.value == D(3800)


def test_explicit_zero_labor_demands_are_known_even_without_a_wage():
    catalog = _catalog()
    catalog.crops[0].operations[0].labor_known = False
    constraints = _constraints(
        wage_mad_per_person_day=None,
        operation_cost_overrides={
            "wheat:work": OperationCostOverride(
                inputs_mad_per_ha=800,
                machinery_mad_per_ha=1000,
                paid_person_days_per_ha=0,
                family_person_days_per_ha=0,
                operator_days_per_ha=0,
            )
        },
    )
    operations, _, financials, _, _ = _budget(catalog=catalog, constraints=constraints)
    assert operations[0].paid_labor_cost_mad == D(0)
    assert financials.whole_plot.operating_cost.value == D(3600)


def test_rented_equipment_operator_is_paid_unless_explicitly_included():
    catalog = _catalog()
    catalog.crops[0].operations[0] = (
        catalog.crops[0]
        .operations[0]
        .model_copy(
            update={
                "labor_known": False,
                "machinery_mode": "rented",
                "included_operator_person_days_per_ha": D(0),
            }
        )
    )
    constraints = _constraints(
        operation_cost_overrides={
            "wheat:work": OperationCostOverride(
                inputs_mad_per_ha=800,
                machinery_mad_per_ha=1000,
                paid_person_days_per_ha=2,
                family_person_days_per_ha=3,
                operator_days_per_ha=1,
            )
        }
    )
    operations, _, financials, labor, _ = _budget(catalog=catalog, constraints=constraints)
    assert labor.paid_person_days == D(6)
    assert labor.included_operator_person_days == D(0)
    assert operations[0].paid_labor_cost_mad == D(600)
    assert financials.whole_plot.operating_cost.value == D(4200)


@pytest.mark.parametrize(
    "missing,expected_yield_reason,expected_price_reason",
    [
        ("cost", "BREAK_EVEN_COST_MISSING", "BREAK_EVEN_COST_MISSING"),
        ("price", "BREAK_EVEN_PRICE_MISSING", None),
        ("yield", None, "BREAK_EVEN_YIELD_MISSING"),
    ],
)
def test_single_crop_break_even_explains_specific_missing_input(
    missing, expected_yield_reason, expected_price_reason
):
    fields = (
        {"operation_cost_overrides": {}}
        if missing == "cost"
        else ({"sale_price_mad_per_kg": {}} if missing == "price" else {"yield_kg_per_ha": {}})
    )
    _, _, financials, _, _ = _budget(constraints=_constraints(**fields))
    for metric, expected in [
        (financials.break_even_yield, expected_yield_reason),
        (financials.break_even_price, expected_price_reason),
    ]:
        assert metric is not None
        if expected is None:
            assert metric.value is not None
        else:
            assert metric.value is None
            assert metric.reason.code == expected
