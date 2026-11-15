"""Inclusive crop windows, evidence-qualified checks, and bounded candidate search."""

from calendar import monthrange
from datetime import date, timedelta

from agrix.models import (
    Availability,
    Catalog,
    Check,
    CropProfile,
    CropSelection,
    EvaluationRequest,
    FarmConstraints,
    Origin,
    Plot,
    PlotProfile,
    Reason,
    Rotation,
    SuggestionRequest,
)


def _date(year: int, month: int, day: int) -> date:
    # A February 29 window in a non-leap year ends on the month's last day.
    return date(year, month, min(day, monthrange(year, month)[1]))


def windows(crop: CropProfile, zone_id: str | None = None) -> list[dict]:
    selected = [w for w in crop.sowing_windows if not w.get("zone_ids") or zone_id in w["zone_ids"]]
    return (
        selected
        if selected
        else (
            []
            if crop.sowing_windows
            else [
                {
                    "start_month": m,
                    "start_day": 1,
                    "end_month": m,
                    "end_day": monthrange(2000, m)[1],
                }
                for m in crop.sowing_months
            ]
        )
    )


def _interval(window: dict, year: int) -> tuple[date, date]:
    start = _date(year, window["start_month"], window["start_day"])
    end_year = year + (
        (window["end_month"], window["end_day"]) < (window["start_month"], window["start_day"])
    )
    return start, _date(end_year, window["end_month"], window["end_day"])


def in_sowing_window(day: date, crop: CropProfile, zone_id: str | None = None) -> bool:
    if crop.fallow:
        return True
    return any(
        start <= day <= end
        for w in windows(crop, zone_id)
        for year in (day.year - 1, day.year)
        if year >= 1
        for start, end in (_interval(w, year),)
    )


def _next_start(earliest: date, crop: CropProfile, zone_id: str | None = None) -> date:
    if crop.fallow:
        return earliest
    choices = []
    for year in range(max(1, earliest.year - 1), min(9999, earliest.year + 2) + 1):
        for window in windows(crop, zone_id):
            crosses_year = (window["end_month"], window["end_day"]) < (
                window["start_month"],
                window["start_day"],
            )
            if year == 9999 and crosses_year:
                continue
            first, last = _interval(window, year)
            if last >= earliest:
                choices.append(max(first, earliest))
    if not choices:
        raise ValueError(f"No supported sowing window for crop {crop.id} in region {zone_id}")
    return min(choices)


def maturity_dates(
    selection: CropSelection, earliest_start: date, crop: CropProfile, zone_id: str | None = None
) -> tuple[date, date]:
    """User dates take precedence. Otherwise use a disclosed nominal duration.

    This is calendar scheduling, never a calibrated maturity/harvest prediction.
    """
    start = selection.start_date or _next_start(earliest_start, crop, zone_id)
    return start, selection.end_date or start + timedelta(days=crop.duration_days - 1)


def _check(
    outcome: str,
    code: str,
    instance_id: str | None = None,
    evidence_ids: list[str] | None = None,
    **values,
) -> Check:
    return Check(
        outcome=outcome,
        reason=Reason(
            code=code, values=values, crop_instance_id=instance_id, evidence_ids=evidence_ids or []
        ),
    )


def _reference_ids(crop: CropProfile, topic: str) -> list[str]:
    return [r["id"] for r in crop.references if topic in r.get("topics", [])]


def rule_checks(
    plot: Plot,
    profile: PlotProfile,
    request: EvaluationRequest,
    catalog: Catalog,
    rotation: Rotation,
) -> list[Check]:
    constraints = request.constraints or plot.constraints
    crops = {c.id: c for c in catalog.crops}
    checks = []
    if profile.rule_coverage != Availability.available:
        checks.append(
            _check(
                "unknown",
                "AGRONOMIC_REGION_CONFIRMATION",
                zone_id=profile.zone_id,
                coverage=profile.rule_coverage.value,
                evidence_ids=["morocco-pilot-region-lookup-v1"],
            )
        )
    checks.append(
        _check(
            "unknown", "ROTATION_DIVERSITY_POLICY", evidence_ids=["morocco-planning-assumptions-v1"]
        )
    )
    previous_ids = constraints.previous_crops
    for index, instance in enumerate(rotation.crops):
        crop = crops[instance.crop_id]
        if crop.fallow:
            checks.append(
                _check(
                    "unknown",
                    "FALLOW_MANAGEMENT_CONFIRMATION",
                    instance.id,
                    evidence_ids=["morocco-planning-assumptions-v1"],
                    duration_days=instance.duration_days,
                )
            )
            # A fallow interval does not by itself establish a disease-breaking interval.
            continue
        covered = profile.zone_id in crop.zone_ids
        if not covered:
            checks.append(
                _check(
                    "unknown",
                    "CROP_RULE_COVERAGE_MISSING",
                    instance.id,
                    crop_id=crop.id,
                    zone_id=profile.zone_id,
                )
            )
        else:
            checks.append(
                _check(
                    "pass"
                    if in_sowing_window(instance.start_date, crop, profile.zone_id)
                    else "fail",
                    "SOWING_WINDOW_MATCH"
                    if in_sowing_window(instance.start_date, crop, profile.zone_id)
                    else "SOWING_WINDOW_MISMATCH",
                    instance.id,
                    evidence_ids=_reference_ids(crop, "sowing"),
                    date=instance.start_date.isoformat(),
                    zone_id=profile.zone_id,
                )
            )
        if constraints.production_system not in crop.production_systems:
            checks.append(
                _check(
                    "unknown",
                    "PRODUCTION_SYSTEM_RULE_MISSING",
                    instance.id,
                    production_system=constraints.production_system,
                )
            )
        checks.append(
            _check(
                "unknown",
                "CROP_DURATION_CONFIRMATION",
                instance.id,
                evidence_ids=["morocco-planning-assumptions-v1"],
                duration_days=instance.duration_days,
                farmer_dates_provided=request.sequence[index].end_date is not None,
            )
        )
        if crop.operations:
            checks.append(
                _check(
                    "unknown",
                    "MANAGEMENT_TIMING_AND_QUANTITY_CONFIRMATION",
                    instance.id,
                    evidence_ids=["morocco-planning-assumptions-v1"],
                )
            )
        # Only barley's explicit cereal-predecessor exclusion is a hard sourced rule.
        predecessors = [rotation.crops[index - 1].crop_id] if index else previous_ids
        if crop.forbidden_predecessors and covered:
            if predecessors is None:
                checks.append(
                    _check(
                        "unknown",
                        "PREVIOUS_CROP_MISSING",
                        instance.id,
                        evidence_ids=_reference_ids(crop, "predecessor"),
                    )
                )
            elif index and crops[predecessors[0]].fallow:
                checks.append(
                    _check(
                        "unknown",
                        "FALLOW_BREAK_DURATION_UNVALIDATED",
                        instance.id,
                        evidence_ids=["morocco-planning-assumptions-v1"],
                    )
                )
            elif any(p in crop.forbidden_predecessors for p in predecessors):
                checks.append(
                    _check(
                        "fail",
                        "DOCUMENTED_PREDECESSOR_EXCLUSION",
                        instance.id,
                        evidence_ids=_reference_ids(crop, "predecessor"),
                        previous_crop_ids=",".join(predecessors),
                    )
                )
            elif any(p not in crops for p in predecessors):
                checks.append(
                    _check(
                        "unknown",
                        "PREVIOUS_CROP_UNMAPPED",
                        instance.id,
                        previous_crop_ids=",".join(predecessors),
                    )
                )
        elif predecessors is None:
            checks.append(_check("unknown", "PREVIOUS_CROP_MISSING", instance.id))
        elif predecessors and all(
            p in crops and crops[p].family == crop.family for p in predecessors
        ):
            checks.append(
                _check(
                    "unknown", "FAMILY_RETURN_INTERVAL_UNVALIDATED", instance.id, family=crop.family
                )
            )
        if crop.ph_min is not None and crop.ph_max is not None and covered:
            ph = (
                profile.metrics.get("soil_ph")
                or profile.metrics.get("ph_h2o")
                or profile.metrics.get("ph")
            )
            reported_tests = [
                t for t in constraints.soil_tests if t.property in ("soil_ph", "ph", "ph_h2o")
            ]
            tests = [
                t
                for t in reported_tests
                if t.sample_date <= date.today() and t.unit in ("pH", "1") and 0 <= t.value <= 14
            ]
            if len(tests) != len(reported_tests):
                checks.append(
                    _check(
                        "unknown",
                        "SOIL_TEST_REQUIRES_CONFIRMATION",
                        instance.id,
                        rejected_test_count=len(reported_tests) - len(tests),
                    )
                )
            test = max(tests, key=lambda item: item.sample_date) if tests else None
            value = test.value if test else (ph.value if ph else None)
            if value is None:
                checks.append(
                    _check(
                        "unknown",
                        "SOIL_PH_MISSING",
                        instance.id,
                        evidence_ids=_reference_ids(crop, "soil"),
                    )
                )
            else:
                within = crop.ph_min <= value <= crop.ph_max
                confirmed = test is not None or (
                    ph.origin in (Origin.observed, Origin.farmer_provided)
                    and ph.availability == Availability.available
                )
                checks.append(
                    _check(
                        "pass" if within else ("fail" if confirmed else "unknown"),
                        "SOIL_PH_WITHIN_GUIDE" if within else "SOIL_PH_CONFIRMATION",
                        instance.id,
                        evidence_ids=_reference_ids(crop, "soil")
                        + (ph.evidence_ids if ph and test is None else []),
                        value=float(value),
                        minimum=float(crop.ph_min),
                        maximum=float(crop.ph_max),
                        source="farmer_soil_test" if test else "profile",
                        sample_date=test.sample_date.isoformat() if test else None,
                        sample_source=test.source_label if test else None,
                        depth_top_cm=test.depth_top_cm if test else None,
                        depth_bottom_cm=test.depth_bottom_cm if test else None,
                    )
                )
        # Annual rain and irrigation capacity are not interchangeable with crop-window supply.
        checks.append(_check("unknown", "CROP_WATER_BALANCE_UNVALIDATED", instance.id))
    return checks


def generate_candidates(
    request: SuggestionRequest, catalog: Catalog, profile: PlotProfile, *, plot: Plot | None = None
) -> list[EvaluationRequest]:
    """Environmental beam search over sowing dates, thermal durations and sequences.

    Per-window environmental screening guides expansion before truncation. The
    final shortlist is rescored with continuous storage across the full rotation.
    Economics never determines which environmental candidates survive the beam.
    """
    from functools import lru_cache

    from agrix.agronomy.environment import EnvironmentalModel, environmental_sort_key

    end = request.start_date + timedelta(days=request.horizon_days - 1)
    constraints = request.constraints or FarmConstraints()
    environment = EnvironmentalModel(
        profile, constraints, request.start_date, request.scenario, plot
    )
    crops = [
        c
        for c in catalog.crops
        if not c.fallow
        and profile.zone_id in c.zone_ids
        and (
            constraints.production_system in c.production_systems
            or (
                constraints.production_system in {"unknown", "irrigated"}
                and "rainfed" in c.production_systems
            )
        )
    ]
    fallow = next((c for c in catalog.crops if c.fallow), None)
    # State: selections, cursor, families, sum of day-weighted conservative scores, productive days.
    beam = [([], request.start_date, [], 0.0, 0)]
    finished = []

    @lru_cache(maxsize=4096)
    def window_score(crop_id, first, last):
        selection = CropSelection(crop_id=crop_id, start_date=first, end_date=last)
        result = environment.assess([selection], catalog)
        establishment = result["crops"][0]["establishment_score"] if result["crops"] else None
        # Wet establishment is part of date choice; its 15% weight is an explicit policy.
        return 0.85 * (result["ranking_score"] or 0) + 15 * (establishment or 0)

    def state_key(state):
        selections, cursor, families, total, days = state
        return (
            -total / max(1, days),
            -len(set(families)),
            -len(families),
            -cursor.toordinal(),
            tuple((x.crop_id, x.start_date) for x in selections),
        )

    for _ in range(20):
        expanded = []
        for selections, cursor, families, score_total, productive_days in beam:
            options = []
            for crop in crops:
                # A generated multi-season rotation alternates crop families. This is a
                # conservative planning policy, not a calibrated disease-return interval.
                if families and crop.family and families[-1] == crop.family:
                    continue
                predecessor = next(
                    (
                        s.crop_id
                        for s in reversed(selections)
                        if s.crop_id != (fallow.id if fallow else None)
                    ),
                    None,
                )
                if predecessor in crop.forbidden_predecessors:
                    continue
                if (
                    not selections
                    and constraints.previous_crops
                    and any(p in crop.forbidden_predecessors for p in constraints.previous_crops)
                ):
                    continue
                try:
                    earliest = _next_start(cursor, crop, profile.zone_id)
                except ValueError:
                    continue
                dated_options = []
                # Compare dates inside the applicable window, not just its first day.
                for delay in (0, 14, 28, 42, 56):
                    first = earliest + timedelta(days=delay)
                    if not in_sowing_window(first, crop, profile.zone_id):
                        continue
                    timing = environment.estimate_duration(crop, first)
                    last = first + timedelta(days=timing["duration_days"] - 1)
                    if last <= end:
                        dated_options.append((window_score(crop.id, first, last), first, last))
                dated_options.sort(key=lambda item: (-item[0], item[1]))
                for score, first, last in dated_options[:2]:
                    interval = []
                    if first > cursor:
                        if fallow is None:
                            continue
                        interval.append(
                            CropSelection(
                                crop_id=fallow.id,
                                start_date=cursor,
                                end_date=first - timedelta(days=1),
                            )
                        )
                    interval.append(
                        CropSelection(
                            crop_id=crop.id,
                            start_date=first,
                            end_date=last,
                            date_policy="environmental",
                        )
                    )
                    if len(selections) + len(interval) <= 20:
                        duration = (last - first).days + 1
                        options.append(
                            (
                                selections + interval,
                                last + timedelta(days=1),
                                families + [crop.family],
                                score_total + score * duration,
                                productive_days + duration,
                            )
                        )
            if options:
                expanded.extend(options)
            elif selections:
                finished.append((selections, cursor, families, score_total, productive_days))
        if not expanded:
            break
        expanded.sort(key=state_key)
        # Reserve space for different crop sequences before adding date variants.
        # Otherwise several dates for the same high-scoring path consume the beam.
        unique_paths, date_variants = {}, []
        for state in expanded:
            path = tuple(
                s.crop_id for s in state[0] if not s.crop_id == (fallow.id if fallow else None)
            )
            if path not in unique_paths:
                unique_paths[path] = state
            else:
                date_variants.append(state)
        beam = [*unique_paths.values(), *date_variants][:32]
    finished.extend(beam)
    results, seen = [], set()
    for selections, cursor, _, _, _ in finished:
        if not selections:
            continue
        if cursor <= end and fallow and len(selections) < 20:
            selections = selections + [
                CropSelection(crop_id=fallow.id, start_date=cursor, end_date=end)
            ]
        key = tuple((s.crop_id, s.start_date, s.end_date) for s in selections)
        if key in seen:
            continue
        seen.add(key)
        assessment = environment.assess(selections, catalog)
        results.append(
            (
                environmental_sort_key(assessment),
                key,
                EvaluationRequest(**request.model_dump(), sequence=selections),
            )
        )
    results.sort(key=lambda item: (item[0], item[1]))
    # Keep only the best dates for an ordered crop rotation. Fallow padding and
    # small sowing-date shifts are not separate recommendations to a farmer.
    lookup = {crop.id: crop for crop in catalog.crops}
    unique = {}
    for item in results:
        path = tuple(s.crop_id for s in item[2].sequence if not lookup[s.crop_id].fallow)
        unique.setdefault(path, item)
    # Prefer different crop combinations over the same pair in reverse order.
    shortlist, repeated_combinations, combinations = [], [], set()
    for path, item in unique.items():
        combination = tuple(sorted(path))
        if combination in combinations:
            repeated_combinations.append(item)
        else:
            combinations.add(combination)
            shortlist.append(item)
    shortlist = sorted((shortlist + repeated_combinations)[:6], key=lambda item: (item[0], item[1]))
    return [item[2] for item in shortlist]
