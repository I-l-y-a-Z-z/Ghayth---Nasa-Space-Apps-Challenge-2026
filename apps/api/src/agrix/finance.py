"""Deterministic operations and cash flow with explicit unknown local inputs.

Money is rounded to cents, and person-days to six decimal places, before line
items are summed. Hired machinery operator time is reported separately from
farm labor and is already covered by the machinery charge. Cash on the same
date is netted: there is no assumed intraday borrowing requirement.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext

from agrix.errors import DomainError
from agrix.models import (
    Availability,
    CashEvent,
    Catalog,
    DateRange,
    FarmConstraints,
    Financials,
    FinancialTotals,
    LaborSummary,
    MachineryRequirement,
    Metric,
    MoneyMetric,
    Operation,
    Origin,
    Reason,
    Rotation,
    Scenario,
    money,
)

ZERO = Decimal(0)
QUANTITY_PRECISION = Decimal("0.000001")


def _quantity(value: Decimal) -> Decimal:
    return value.quantize(QUANTITY_PRECISION, rounding=ROUND_HALF_UP)


def _metric(
    value: Decimal | None,
    *,
    per_ha: bool = False,
    reason: str = "BUDGET_EVIDENCE_MISSING",
    evidence_id: str = "fixture-budget-v1",
) -> MoneyMetric:
    return MoneyMetric(
        value=value,
        unit="MAD/ha" if per_ha else "MAD",
        availability=Availability.available if value is not None else Availability.missing,
        reason=Reason(code=reason) if value is None else None,
        evidence_ids=[evidence_id],
    )


def _funding_metrics(
    events: list[CashEvent], first_receipt: date | None
) -> tuple[Decimal, Decimal]:
    """Calculate whole-day cash shortfalls; callers must supply known amounts."""
    dated: dict[date, Decimal] = defaultdict(Decimal)
    for event in events:
        assert event.amount_mad is not None
        dated[event.date] += event.amount_mad * (1 if event.direction == "inflow" else -1)
    balance = ZERO
    peak = ZERO
    before_first = ZERO
    for cash_date, amount in sorted(dated.items()):
        balance = money(balance + amount)
        shortfall = max(ZERO, -balance)
        peak = max(peak, shortfall)
        if first_receipt is None or cash_date < first_receipt:
            before_first = max(before_first, shortfall)
    return money(before_first), money(peak)


def build_budget(
    rotation: Rotation,
    catalog: Catalog,
    constraints: FarmConstraints,
    area_ha: Decimal,
    scenario: Scenario,
    allow_catalog_assumptions: bool,
) -> tuple[list[Operation], list[CashEvent], Financials, LaborSummary, list[MachineryRequirement]]:
    """Expand fixture operations and return traceable, cent-exact cash summaries.

    An unavailable regional rule disables catalog yield and price defaults, while
    explicit farmer overrides remain usable. Unknown sale amounts leave revenue,
    margins and funding unknown. The date of the first scheduled sale includes a
    known zero sale; it remains available even when its amount is unknown.

    The existing contract's ``money`` validator enforces the same finite maximum
    for every computed monetary amount and aggregate as for API money inputs.
    """
    if isinstance(area_ha, bool):
        raise ValueError("area_ha must be a positive finite Decimal")
    area = Decimal(str(area_ha))
    if not area.is_finite() or area <= 0:
        raise ValueError("area_ha must be a positive finite Decimal")

    # Bounded API factors can have many digits when multiplied; retain precision
    # until explicit quantity/currency rounding rather than ambient context loss.
    with localcontext() as context:
        context.prec = 80
        return _build_budget(
            rotation, catalog, constraints, area, scenario, allow_catalog_assumptions
        )


def _build_budget(
    rotation: Rotation,
    catalog: Catalog,
    constraints: FarmConstraints,
    area: Decimal,
    scenario: Scenario,
    allow_catalog_assumptions: bool,
) -> tuple[list[Operation], list[CashEvent], Financials, LaborSummary, list[MachineryRequirement]]:
    crops = {crop.id: crop for crop in catalog.crops}
    budget_evidence = (
        "fixture-budget-v1"
        if catalog.synthetic
        else "local-economic-references-v1"
        if constraints.economic_evidence_ids
        else "farmer-budget-v1"
    )
    catalog_evidence = "fixture-catalog-v1" if catalog.synthetic else "agronomic-catalog-v1"
    operations: list[Operation] = []
    events: list[CashEvent] = []
    machines: list[MachineryRequirement] = []
    farm_by_month: dict[str, Decimal | None] = defaultdict(Decimal)
    paid_days = family_days = operator_days = ZERO
    paid_cost: Decimal | None = ZERO
    all_labor_known = True

    for instance in rotation.crops:
        if instance.crop_id not in crops:
            raise ValueError(f"Unknown crop ID: {instance.crop_id}")
        crop = crops[instance.crop_id]
        seasonal_reference = constraints.seasonal_operating_cost_mad_per_ha.get(crop.id)
        if seasonal_reference is not None and not crop.fallow:
            events.append(
                CashEvent(
                    id=f"cash:{instance.id}:season-reference",
                    date=instance.start_date,
                    direction="outflow",
                    kind="operating",
                    amount_mad=money(seasonal_reference * area),
                    crop_instance_id=instance.id,
                    synthetic=False,
                    description="Historical whole-season cost reference reserved at sowing; detailed payment dates unavailable",
                    evidence_ids=constraints.economic_evidence_ids,
                )
            )
        for template in crop.operations:
            try:
                operation_date = (
                    instance.end_date
                    if not catalog.synthetic and template.id == "harvest"
                    else instance.start_date + timedelta(days=template.offset_days)
                )
            except OverflowError as exc:
                raise ValueError("Operation date exceeds the supported calendar") from exc
            if not instance.start_date <= operation_date <= instance.end_date:
                raise DomainError(
                    "OPERATION_TIMING_REQUIRES_CONFIRMATION",
                    "Selected crop dates cannot contain the operation calendar; confirm a longer crop duration",
                )
            override = constraints.operation_cost_overrides.get(f"{crop.id}:{template.id}")
            paid_rate = template.paid_person_days_per_ha
            family_rate = template.family_person_days_per_ha
            operator_rate = template.included_operator_person_days_per_ha
            labor_known = template.labor_known
            if override is not None:
                overrides = (
                    override.paid_person_days_per_ha,
                    override.family_person_days_per_ha,
                    override.operator_days_per_ha,
                )
                labor_known = labor_known or all(value is not None for value in overrides)
                paid_rate = paid_rate if overrides[0] is None else overrides[0]
                family_rate = family_rate if overrides[1] is None else overrides[1]
                operator_rate = operator_rate if overrides[2] is None else overrides[2]
            paid = _quantity(paid_rate * area)
            family = _quantity(family_rate * area)
            operator = _quantity(operator_rate * area)
            mode = template.machinery_mode
            inputs_rate = template.inputs_mad_per_ha if template.costs_known else None
            machine_rate = (
                template.machinery_mad_per_ha
                if template.costs_known
                else (ZERO if template.machinery_id is None else None)
            )
            if override is not None:
                if override.inputs_mad_per_ha is not None:
                    inputs_rate = override.inputs_mad_per_ha
                if override.machinery_mad_per_ha is not None:
                    machine_rate = override.machinery_mad_per_ha
            equipment = constraints.equipment.get(template.machinery_id or "")
            if equipment:
                mode = equipment.mode
                if equipment.cost_mad_per_ha is not None:
                    machine_rate = equipment.cost_mad_per_ha
                elif mode in {"owned", "shared"}:
                    machine_rate = ZERO  # no rental fee; fuel/maintenance remain input costs
                elif mode in {"unknown", "unavailable"}:
                    machine_rate = None
            included = mode == "hired_with_operator" or bool(
                equipment
                and (equipment.operator_included is True or equipment.operator_labor == "included")
            )
            if not included:
                if equipment and equipment.operator_labor == "family":
                    family += operator
                else:
                    paid += operator
                operator = ZERO
            inputs = money(inputs_rate * area) if inputs_rate is not None else None
            machinery = money(machine_rate * area) if machine_rate is not None else None
            wages = (
                None
                if not labor_known
                else ZERO
                if paid == ZERO
                else (
                    money(paid * constraints.wage_mad_per_person_day)
                    if constraints.wage_mad_per_person_day is not None
                    else None
                )
            )
            total = (
                money(inputs + machinery + wages)
                if all(x is not None for x in (inputs, machinery, wages))
                else None
            )
            operation_id = f"{instance.id}:{template.id}"
            operation = Operation(
                id=operation_id,
                crop_instance_id=instance.id,
                code=template.id,
                date=operation_date,
                booking_window=DateRange(
                    start=date.fromordinal(
                        max(date.min.toordinal(), operation_date.toordinal() - 14)
                    ),
                    end=operation_date,
                ),
                area_ha=area,
                inputs_cost_mad=inputs,
                machinery_cost_mad=machinery,
                paid_labor_cost_mad=wages,
                total_cash_cost_mad=total,
                paid_person_days=paid if labor_known else None,
                family_person_days=family if labor_known else None,
                included_operator_person_days=operator if labor_known else None,
                machinery_id=template.machinery_id,
                machinery_mode=mode,
                evidence_ids=[catalog_evidence, budget_evidence, *template.source_ids],
                synthetic=catalog.synthetic,
                quantities=[
                    {
                        **q,
                        "quantity": (
                            str(_quantity(Decimal(str(q["per_ha"])) * area))
                            if q.get("per_ha") is not None
                            else None
                        ),
                    }
                    for q in template.quantities
                ],
            )
            operations.append(operation)
            if seasonal_reference is None:
                events.append(
                    CashEvent(
                        id=f"cash:{operation_id}",
                        date=operation_date,
                        direction="outflow",
                        kind="operating",
                        amount_mad=total,
                        crop_instance_id=instance.id,
                        operation_id=operation_id,
                        availability=Availability.available
                        if total is not None
                        else Availability.missing,
                        reason=Reason(
                            code="OPERATION_COST_MISSING",
                            values={"operation": template.id, "labor_missing": not labor_known},
                        )
                        if total is None
                        else None,
                        evidence_ids=[budget_evidence],
                        synthetic=catalog.synthetic,
                    )
                )
            if template.machinery_id is not None:
                machines.append(
                    MachineryRequirement(
                        operation_id=operation_id,
                        machinery_id=template.machinery_id,
                        mode=mode,
                        operator_included=included,
                        cost_mad=machinery,
                    )
                )
            paid_days += paid
            family_days += family
            operator_days += operator
            all_labor_known = all_labor_known and labor_known
            paid_cost = (
                money(paid_cost + wages) if paid_cost is not None and wages is not None else None
            )
            month = operation_date.strftime("%Y-%m")
            current = farm_by_month[month]
            farm_by_month[month] = (
                current + paid + family if current is not None and labor_known else None
            )

        if crop.fallow:
            continue
        yield_per_ha = constraints.yield_kg_per_ha.get(crop.id)
        price_per_kg = constraints.sale_price_mad_per_kg.get(crop.id)
        if allow_catalog_assumptions:
            if yield_per_ha is None:
                yield_per_ha = crop.yield_kg_per_ha
            if price_per_kg is None:
                price_per_kg = crop.sale_price_mad_per_kg

        amount = None
        reason = None
        if yield_per_ha is not None and price_per_kg is not None:
            quantity = _quantity(yield_per_ha * scenario.yield_multiplier * area)
            adjusted_price = money(price_per_kg * scenario.price_multiplier)
            amount = money(quantity * adjusted_price)
        else:
            reason = Reason(
                code="YIELD_OR_PRICE_MISSING",
                crop_instance_id=instance.id,
                values={
                    "crop_id": crop.id,
                    "yield_missing": yield_per_ha is None,
                    "price_missing": price_per_kg is None,
                    "catalog_assumptions_allowed": allow_catalog_assumptions,
                },
            )
        try:
            receipt_date = instance.end_date + timedelta(days=constraints.payment_lag_days)
        except OverflowError as exc:
            raise ValueError("Payment date exceeds the supported calendar") from exc
        events.append(
            CashEvent(
                id=f"cash:{instance.id}:sale",
                date=receipt_date,
                direction="inflow",
                kind="sale",
                amount_mad=amount,
                availability=Availability.available if amount is not None else Availability.missing,
                reason=reason,
                crop_instance_id=instance.id,
                evidence_ids=[budget_evidence, *scenario.evidence_ids],
                synthetic=catalog.synthetic,
            )
        )

    operations.sort(key=lambda operation: (operation.date, operation.id))
    setup_date = operations[0].date if operations else rotation.start_date
    events.append(
        CashEvent(
            id="cash:setup",
            date=setup_date,
            direction="outflow",
            kind="setup",
            amount_mad=constraints.setup_cost_mad,
            availability=Availability.available
            if constraints.setup_cost_mad is not None
            else Availability.missing,
            reason=Reason(code="SETUP_COST_UNKNOWN")
            if constraints.setup_cost_mad is None
            else None,
            evidence_ids=[budget_evidence],
            synthetic=catalog.synthetic,
        )
    )
    for extra in constraints.additional_costs:
        events.append(
            CashEvent(
                id=f"cash:additional:{extra.id}",
                date=extra.date,
                direction="outflow",
                kind=extra.kind,
                amount_mad=extra.amount_mad,
                description=extra.label,
                evidence_ids=[budget_evidence],
                synthetic=catalog.synthetic,
            )
        )
    events.sort(key=lambda event: (event.date, event.id))

    op_events = [event for event in events if event.kind == "operating"]
    setup_events = [event for event in events if event.kind == "setup"]
    operating = (
        money(sum((e.amount_mad for e in op_events), ZERO))
        if all(e.amount_mad is not None for e in op_events)
        else None
    )
    investment = (
        money(sum((e.amount_mad for e in setup_events), ZERO))
        if all(e.amount_mad is not None for e in setup_events)
        else None
    )
    sales = [event for event in events if event.kind == "sale"]
    first_receipt = min((event.date for event in sales), default=None)
    known_revenue = all(event.amount_mad is not None for event in sales)
    revenue = (
        money(sum((event.amount_mad for event in sales if event.amount_mad is not None), ZERO))
        if known_revenue
        else None
    )
    margin = money(revenue - operating) if revenue is not None and operating is not None else None
    net = (
        money(revenue - operating - investment)
        if all(x is not None for x in (revenue, operating, investment))
        else None
    )
    before_first, peak = (
        _funding_metrics(events, first_receipt)
        if all(e.amount_mad is not None for e in events)
        else (None, None)
    )
    values = dict(
        initial_investment=investment,
        operating_cost=operating,
        expected_revenue=revenue,
        operating_margin=margin,
        net_cash_flow=net,
        cash_before_first_receipt=before_first,
        peak_funding_need=peak,
    )
    financials = Financials(
        whole_plot=FinancialTotals(
            **{
                name: _metric(
                    value,
                    evidence_id=budget_evidence,
                    reason="REVENUE_EVIDENCE_MISSING"
                    if not known_revenue
                    else "OPERATION_COST_MISSING",
                )
                for name, value in values.items()
            }
        ),
        per_ha=FinancialTotals(
            **{
                name: _metric(
                    money(value / area) if value is not None else None,
                    per_ha=True,
                    evidence_id=budget_evidence,
                    reason="REVENUE_EVIDENCE_MISSING"
                    if not known_revenue
                    else "OPERATION_COST_MISSING",
                )
                for name, value in values.items()
            }
        ),
        first_receipt_date=first_receipt,
        last_cash_event_date=max(event.date for event in events),
        break_even_yield=Metric(
            value=None,
            unit="kg/ha",
            availability=Availability.inapplicable,
            origin=Origin.assumption,
            reason=Reason(code="MULTI_CROP_BREAK_EVEN_NOT_DEFINED"),
            evidence_ids=[budget_evidence],
        ),
    )
    non_fallow = [item for item in rotation.crops if not crops[item.crop_id].fallow]
    if len(non_fallow) == 1:
        crop = crops[non_fallow[0].crop_id]
        price = constraints.sale_price_mad_per_kg.get(crop.id)
        yield_value = constraints.yield_kg_per_ha.get(crop.id)
        if allow_catalog_assumptions:
            price = crop.sale_price_mad_per_kg if price is None else price
            yield_value = crop.yield_kg_per_ha if yield_value is None else yield_value
        adjusted_price = money(price * scenario.price_multiplier) if price is not None else None
        adjusted_yield = (
            _quantity(yield_value * scenario.yield_multiplier * area) / area
            if yield_value is not None
            else None
        )

        def gap(unit: str, code: str, *, inapplicable: bool = False) -> Metric:
            return Metric(
                value=None,
                unit=unit,
                availability=Availability.inapplicable if inapplicable else Availability.missing,
                origin=Origin.assumption,
                reason=Reason(code=code),
                evidence_ids=[budget_evidence],
            )

        financials.break_even_yield = (
            gap("kg/ha", "BREAK_EVEN_COST_MISSING")
            if operating is None
            else gap("kg/ha", "BREAK_EVEN_PRICE_MISSING")
            if adjusted_price is None
            else gap("kg/ha", "BREAK_EVEN_PRICE_NONPOSITIVE", inapplicable=True)
        )
        financials.break_even_price = (
            gap("MAD/kg", "BREAK_EVEN_COST_MISSING")
            if operating is None
            else gap("MAD/kg", "BREAK_EVEN_YIELD_MISSING")
            if adjusted_yield is None
            else gap("MAD/kg", "BREAK_EVEN_YIELD_NONPOSITIVE", inapplicable=True)
        )
        if operating is not None and adjusted_price is not None and adjusted_price > ZERO:
            financials.break_even_yield = Metric(
                value=_quantity(operating / area / adjusted_price),
                unit="kg/ha",
                availability=Availability.available,
                origin=Origin.assumption,
                evidence_ids=[budget_evidence],
            )
        if operating is not None and adjusted_yield is not None and adjusted_yield > ZERO:
            financials.break_even_price = Metric(
                value=money(operating / area / adjusted_yield),
                unit="MAD/kg",
                availability=Availability.available,
                origin=Origin.assumption,
                evidence_ids=[budget_evidence],
            )
    labor = LaborSummary(
        paid_person_days=_quantity(paid_days) if all_labor_known else None,
        family_person_days=_quantity(family_days) if all_labor_known else None,
        included_operator_person_days=_quantity(operator_days) if all_labor_known else None,
        paid_cost_mad=paid_cost,
        farm_person_days_by_month={
            month: _quantity(days) if days is not None else None
            for month, days in sorted(farm_by_month.items())
        },
    )
    return operations, events, financials, labor, machines
