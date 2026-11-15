"""Shared, evidence-qualified evaluation for live and explicitly synthetic plans."""

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from agrix.errors import DomainError
from agrix.finance import build_budget
from agrix.models import (
    Availability,
    Catalog,
    Check,
    CropInstance,
    CropSelection,
    DateRange,
    Evaluation,
    EvaluationInputs,
    EvaluationRequest,
    Evidence,
    MarketObservation,
    Origin,
    Plot,
    PlotProfile,
    Reason,
    Rotation,
    Section,
    SuggestionRequest,
    Supplier,
)


def make_rotation(
    request: EvaluationRequest, catalog: Catalog, zone_id: str | None = None, *, environment=None
) -> Rotation:
    crops = {crop.id: crop for crop in catalog.crops}
    instances = []
    cursor = request.start_date
    for index, selection in enumerate(request.sequence):
        if selection.crop_id not in crops:
            raise DomainError("UNKNOWN_CROP", f"Unknown crop ID: {selection.crop_id}")
        crop = crops[selection.crop_id]
        if catalog.synthetic:
            start = selection.start_date or cursor
            end = selection.end_date or start + timedelta(days=crop.duration_days - 1)
        else:
            from agrix.agronomy import maturity_dates

            try:
                start, end = maturity_dates(selection, cursor, crop, zone_id)
                if environment is not None and not crop.fallow and selection.end_date is None:
                    timing = environment.estimate_duration(crop, start)
                    end = start + timedelta(days=timing["duration_days"] - 1)
            except ValueError as exc:
                raise DomainError(
                    "NO_APPLICABLE_CROP_CALENDAR",
                    "No applicable regional crop calendar; supply explicit crop dates",
                ) from exc
        if end < start:
            raise DomainError("CROP_DATE_CONFLICT", "Harvest must follow sowing")
        if catalog.synthetic and (end - start).days + 1 != crop.duration_days:
            raise DomainError(
                "CROP_DURATION_CONFLICT",
                f"{crop.id} requires {crop.duration_days} days in this fixture catalog",
            )
        instances.append(
            CropInstance(
                id=f"season-{index + 1}-{crop.id}",
                crop_id=crop.id,
                start_date=start,
                end_date=end,
                duration_days=(end - start).days + 1,
                timing=(
                    {
                        **environment.estimate_duration(crop, start),
                        "selected_duration_days": (end - start).days + 1,
                        "date_policy": selection.date_policy,
                        "explicit_end_preserved": selection.end_date is not None,
                    }
                    if environment is not None and not crop.fallow
                    else {}
                ),
            )
        )
        cursor = end + timedelta(days=1)
    start = min(c.start_date for c in instances)
    end = max(c.end_date for c in instances)
    return Rotation(
        crops=instances, start_date=start, end_date=end, duration_days=(end - start).days + 1
    )


def evaluate(
    plot: Plot,
    profile: PlotProfile,
    request: EvaluationRequest,
    catalog: Catalog,
    *,
    analysis_id: str,
) -> Evaluation:
    """Use exactly this path for suggestions and custom input."""
    if request.plot_id != plot.id or request.plot_revision != plot.revision:
        raise DomainError("STALE_PLOT_REVISION", "Evaluate the current plot revision", 409)
    if profile.plot_id != plot.id or profile.plot_revision != plot.revision:
        raise DomainError("STALE_ANALYSIS", "Profile does not match the current plot", 409)
    constraints = request.constraints or plot.constraints
    resolved = request.model_copy(update={"constraints": constraints})
    environment = None
    if not catalog.synthetic and any(c.environmental_parameters for c in catalog.crops):
        from agrix.agronomy.environment import EnvironmentalModel

        environment = EnvironmentalModel(
            profile, constraints, request.start_date, request.scenario, plot
        )
    rotation = make_rotation(resolved, catalog, profile.zone_id, environment=environment)
    environmental = environment.assess(rotation.crops, catalog) if environment else None
    lookup = {crop.id: crop for crop in catalog.crops}
    checks: list[Check] = []

    def check(
        outcome: str,
        code: str,
        instance_id: str | None = None,
        *,
        evidence_ids: list[str] | None = None,
        **values,
    ):
        checks.append(
            Check(
                outcome=outcome,
                reason=Reason(
                    code=code,
                    values=values,
                    crop_instance_id=instance_id,
                    evidence_ids=evidence_ids
                    if evidence_ids is not None
                    else ["fixture-catalog-v1" if catalog.synthetic else "agronomic-catalog-v1"],
                ),
            )
        )

    supported = profile.rule_coverage == Availability.available
    if catalog.synthetic:
        check(
            "pass" if supported else "unknown",
            "FIXTURE_RULE_AVAILABLE" if supported else "NO_APPLICABLE_CROP_RULE",
            zone_id=profile.zone_id,
        )
    else:
        from agrix.agronomy import rule_checks

        checks.extend(rule_checks(plot, profile, resolved, catalog, rotation))
        if environmental is not None:
            check(
                "unknown",
                "ENVIRONMENTAL_SCREENING_REQUIRES_LOCAL_VALIDATION",
                evidence_ids=environmental["evidence_ids"],
                score=environmental["score"],
                coverage=environmental["coverage"],
                model_version=environmental["version"],
            )
        for component in environmental["components"] if environmental else []:
            check(
                "pass"
                if component["score"] is not None and component["coverage"] == 1
                else "unknown",
                "ENVIRONMENTAL_INPUT_USED"
                if component["score"] is not None
                else "ENVIRONMENTAL_INPUT_MISSING",
                evidence_ids=component["evidence_ids"],
                metric=component["id"],
                score=component["score"],
                coverage=component["coverage"],
            )
            if (
                component["score"] is not None
                and component["score"] < 40
                and component["coverage"] >= 0.8
            ):
                check(
                    "unknown",
                    "ENVIRONMENTAL_RISK",
                    evidence_ids=component["evidence_ids"],
                    metric=component["id"],
                    score=component["score"],
                )
    for key in ("rainfall_mm", "soil_ph") if catalog.synthetic else ():
        metric = profile.metrics[key]
        known = metric.availability == Availability.available
        check(
            "pass" if known else "unknown",
            "PROFILE_INPUT_AVAILABLE" if known else "PROFILE_INPUT_MISSING",
            evidence_ids=metric.evidence_ids,
            metric=key,
        )
    horizon_end = request.start_date + timedelta(days=request.horizon_days - 1)
    previous = None
    for instance in rotation.crops:
        crop = lookup[instance.crop_id]
        applicable = catalog.synthetic and supported and profile.zone_id in crop.zone_ids
        if catalog.synthetic:
            check(
                "pass" if applicable else "unknown",
                "CROP_RULE_APPLIES" if applicable else "CROP_RULE_NOT_APPLICABLE",
                instance.id,
                crop_id=crop.id,
            )
        if applicable:
            check(
                "pass" if instance.start_date.month in crop.sowing_months else "fail",
                "SOWING_WINDOW_FITS"
                if instance.start_date.month in crop.sowing_months
                else "SOWING_WINDOW_CONFLICT",
                instance.id,
                month=instance.start_date.month,
            )
            check(
                "pass"
                if constraints.production_system in crop.production_systems
                else ("unknown" if constraints.production_system == "unknown" else "fail"),
                "PRODUCTION_SYSTEM_CHECK",
                instance.id,
                production_system=constraints.production_system,
            )
            check(
                "pass" if instance.duration_days == crop.duration_days else "fail",
                "CROP_DURATION_CHECK",
                instance.id,
                actual_days=instance.duration_days,
                fixture_days=crop.duration_days,
            )
        fits_dates = instance.start_date >= request.start_date and instance.end_date <= horizon_end
        check(
            "pass" if fits_dates else "fail",
            "PLANNING_HORIZON_CHECK",
            instance.id,
            horizon_end=horizon_end.isoformat(),
        )
        if previous is not None:
            check(
                "pass" if instance.start_date > previous.end_date else "fail",
                "SEASON_OVERLAP_CHECK",
                instance.id,
            )
        predecessors = [previous.crop_id] if previous else (constraints.previous_crops or [])
        if applicable and predecessors:
            conflict = any(c in crop.forbidden_predecessors for c in predecessors)
            check(
                "fail" if conflict else "pass",
                "PREDECESSOR_CHECK",
                instance.id,
                predecessor=predecessors[-1],
            )
        elif applicable and previous is None and constraints.previous_crops is None:
            check("unknown", "PREVIOUS_CROP_UNKNOWN", instance.id)
        previous = instance

    operations, events, financials, labor, machinery = build_budget(
        rotation,
        catalog,
        constraints,
        plot.area_ha,
        request.scenario,
        supported and catalog.synthetic,
    )
    if machinery and not constraints.equipment:
        machine_state = constraints.machinery_access
        check(
            {"available": "pass", "unavailable": "fail", "unknown": "unknown"}[machine_state],
            "MACHINERY_ACCESS_CHECK",
            access=machine_state,
        )
    for machine_id in (
        sorted({item.machinery_id for item in machinery}) if constraints.equipment else []
    ):
        equipment = constraints.equipment.get(machine_id)
        access = equipment.mode if equipment else constraints.machinery_access
        check(
            "fail" if access == "unavailable" else "unknown" if access == "unknown" else "pass",
            "MACHINERY_ACCESS_CHECK",
            machine_id=machine_id,
            access=access,
        )
    labor_complete = all(
        value is not None
        for value in (
            labor.paid_person_days,
            labor.family_person_days,
            labor.included_operator_person_days,
            *labor.farm_person_days_by_month.values(),
        )
    )
    labor_need = sum(labor.farm_person_days_by_month.values()) if labor_complete else None
    if constraints.labor_person_days_available is not None:
        check(
            "unknown"
            if labor_need is None
            else ("pass" if constraints.labor_person_days_available >= labor_need else "fail"),
            "LABOR_CAPACITY_CHECK",
            required_person_days=str(labor_need) if labor_need is not None else None,
            available_person_days=str(constraints.labor_person_days_available),
        )
    elif not constraints.labor_by_month:
        check("unknown", "LABOR_CAPACITY_UNKNOWN")
    for month, required in labor.farm_person_days_by_month.items():
        if constraints.labor_by_month:
            available = constraints.labor_by_month.get(month)
            check(
                "unknown"
                if available is None or required is None
                else ("pass" if available >= required else "fail"),
                "MONTHLY_LABOR_CHECK",
                month=month,
                required_person_days=str(required) if required is not None else None,
                available_person_days=str(available) if available is not None else None,
            )
    peak = financials.whole_plot.peak_funding_need.value
    budget = constraints.budget_mad
    if budget is not None or catalog.synthetic:
        check(
            "unknown" if budget is None or peak is None else ("pass" if budget >= peak else "fail"),
            "BUDGET_CHECK",
            evidence_ids=["fixture-budget-v1" if catalog.synthetic else "farmer-budget-v1"],
            budget_mad=str(budget) if budget is not None else None,
            required_mad=str(peak) if peak is not None else None,
        )
    if financials.whole_plot.operating_cost.value is None:
        check("unknown", "OPERATION_COST_MISSING", evidence_ids=["farmer-budget-v1"])
    if not labor_complete:
        check(
            "unknown",
            "LABOR_REQUIREMENTS_NEED_LOCAL_CONFIRMATION",
            evidence_ids=["morocco-planning-assumptions-v1"],
        )
    climate = []
    if not catalog.synthetic:
        from agrix.agronomy import climate_exposure

        climate = climate_exposure(
            rotation, profile, request.scenario, planning_start=request.start_date
        )
        check(
            "pass"
            if climate and all(c["availability"] == "available" for c in climate)
            else "unknown",
            "HISTORICAL_CLIMATE_COVERAGE",
            evidence_ids=[e.id for e in profile.evidence],
        )
    outcomes = {item.outcome for item in checks}
    status = (
        "doesnt_fit"
        if "fail" in outcomes
        else ("needs_confirmation" if "unknown" in outcomes else "fits_current_inputs")
    )
    snapshot = EvaluationInputs(
        request=resolved, area_ha=plot.area_ha, profile=profile, catalog=catalog
    ).model_copy(deep=True)
    fingerprint = hashlib.sha256(
        json.dumps(snapshot.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    evidence = [item.model_copy(deep=True) for item in profile.evidence] + [
        Evidence(id=eid, source_id=eid, source_version=catalog.version, origin=Origin.assumption)
        for eid in (
            (
                "fixture-catalog-v1",
                "fixture-budget-v1",
                "fixture-scenario-v1",
                "fixture-market-v1",
            )
            if catalog.synthetic
            else ()
        )
    ]
    if not catalog.synthetic:
        from agrix.agronomy import reference_evidence

        evidence.extend(reference_evidence(catalog))
        evidence.extend(
            [
                Evidence(
                    id="agronomic-catalog-v1",
                    source_id="agrix-qualified-rule-catalog",
                    source_version=catalog.version,
                    origin=Origin.modeled,
                    synthetic=False,
                ),
                Evidence(
                    id="local-economic-references-v1"
                    if constraints.economic_evidence_ids
                    else "farmer-budget-v1",
                    source_id="published-Moroccan-economic-references"
                    if constraints.economic_evidence_ids
                    else "farmer-inputs-and-local-quotes",
                    source_version="1",
                    origin=Origin.assumption
                    if constraints.economic_evidence_ids
                    else Origin.farmer_provided,
                    synthetic=False,
                    quality_notes=[
                        "Missing costs and yields remain unknown; supplied estimates are not validated forecasts."
                    ],
                ),
            ]
        )
        if constraints.soil_tests:
            evidence.append(
                Evidence(
                    id="farmer-soil-input-v2",
                    source_id="farmer-supplied-soil-tests",
                    source_version="2",
                    origin=Origin.farmer_provided,
                    synthetic=False,
                    quality_notes=[
                        "Exact property, date and depth are retained in the input snapshot."
                    ],
                )
            )
    known_ids = {e.id for e in evidence}
    for eid in request.scenario.evidence_ids:
        if eid not in known_ids:
            evidence.append(
                Evidence(
                    id=eid,
                    source_id="explicit-scenario-assumptions",
                    source_version=request.scenario.id,
                    origin=Origin.assumption,
                    synthetic=catalog.synthetic,
                )
            )
    if supported and catalog.synthetic:
        market = Section[list[MarketObservation]](
            data=[
                MarketObservation(
                    crop_id=cid,
                    date=date(2024, 1, 1),
                    price_mad_per_kg=lookup[cid].sale_price_mad_per_kg,
                    locality=profile.zone_id,
                )
                for cid in sorted({c.crop_id for c in rotation.crops})
            ]
        )
        suppliers = Section[list[Supplier]](
            data=[
                Supplier(
                    id="supplier-fictional-1",
                    name="Demo Farm Services — fictional",
                    service="machinery",
                    locality=profile.zone_id,
                    verification_date=date(2024, 1, 1),
                )
            ]
        )
    else:
        gap = Reason(code="LOCAL_MARKET_EVIDENCE_MISSING")
        market = Section[list[MarketObservation]](availability=Availability.missing, reason=gap)
        suppliers = Section[list[Supplier]](
            availability=Availability.missing, reason=Reason(code="LOCAL_SUPPLIERS_MISSING")
        )
    return Evaluation(
        id=str(uuid4()),
        project_id=plot.project_id,
        plot_id=plot.id,
        plot_revision=plot.revision,
        analysis_id=analysis_id,
        fixture_version=profile.fixture_version,
        catalog_version=catalog.version,
        fingerprint=fingerprint,
        created_at=datetime.now(timezone.utc),
        status=status,
        rotation=rotation,
        checks=checks,
        pros=[c.reason for c in checks if c.outcome == "pass"],
        cons=[c.reason for c in checks if c.outcome != "pass"],
        timeline=Section(data=operations),
        cash_events=events,
        financials=financials,
        market_history=market,
        suppliers=suppliers,
        machinery=Section(data=machinery),
        labor=Section(
            data=labor,
            availability=Availability.partial
            if not labor_complete or labor.paid_cost_mad is None
            else Availability.available,
            reason=Reason(code="LABOR_REQUIREMENTS_NEED_LOCAL_CONFIRMATION")
            if not labor_complete
            else Reason(code="LABOR_COST_UNKNOWN")
            if labor.paid_cost_mad is None
            else None,
        ),
        evidence=evidence,
        input_snapshot=snapshot,
        synthetic=catalog.synthetic,
        climate_scenarios=climate,
        comparison_period=DateRange(start=request.start_date, end=horizon_end),
        next_actions=[c.reason for c in checks if c.outcome != "pass"],
        environmental=environmental,
    )


def candidate_requests(request: SuggestionRequest, catalog: Catalog) -> list[EvaluationRequest]:
    """Generate dated fixture templates, stopping at the configured planning horizon."""
    lookup = {crop.id: crop for crop in catalog.crops}
    horizon_end = request.start_date + timedelta(days=request.horizon_days - 1)
    result = []
    for template in (("wheat_demo", "faba_demo"), ("barley_demo", "faba_demo")):
        selections = []
        cursor = request.start_date
        for i in range(10):
            crop = lookup[template[i % len(template)]]
            # November is an invented fixture window, not a national planting rule.
            start = (
                cursor
                if cursor.month in crop.sowing_months
                else date(cursor.year + (cursor.month > 11), 11, 1)
            )
            end = start + timedelta(days=crop.duration_days - 1)
            if end > horizon_end:
                break
            selections.append(CropSelection(crop_id=crop.id, start_date=start, end_date=end))
            cursor = end + timedelta(days=1)
        if selections:
            result.append(EvaluationRequest(**request.model_dump(), sequence=selections))
    return result
