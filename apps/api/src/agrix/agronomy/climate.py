"""Historical calendar replay, with independent missingness for each variable.

These are exposure diagnostics. They do not predict a season or calibrate yield.
"""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from agrix.errors import DomainError
from agrix.models import PlotProfile, Rotation, Scenario


def _mapped(day: date, anchor_year: int, original_year: int) -> date:
    year = anchor_year + day.year - original_year
    return date(year, day.month, min(day.day, monthrange(year, day.month)[1]))


def _days(start: date, end: date):
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def _window(start: date, end: date, weather: dict, scenario: Scenario) -> dict:
    days = list(_days(start, end))
    rows = [weather.get(day) for day in days]
    rains = [row.rain_mm if row else None for row in rows]
    highs = [row.tmax_c if row else None for row in rows]
    lows = [row.tmin_c if row else None for row in rows]
    n = len(days)
    valid_rain = sum(v is not None for v in rains)
    valid_high = sum(v is not None for v in highs)
    valid_low = sum(v is not None for v in lows)
    rainfall = sum((v for v in rains if v is not None), Decimal(0))
    dry_run, longest = 0, 0
    for rain in rains:
        if rain is None or rain >= scenario.dry_day_threshold_mm:
            dry_run = 0
        else:
            dry_run += 1
            longest = max(longest, dry_run)
    # Analysis-only thresholds: unvalidated for crop/cultivar sensitivity.
    heat_count = sum(value is not None and value > 35 for value in highs)
    frost_count = sum(value is not None and value < 0 for value in lows)
    onset, earlier_gap = None, False
    onset_days = min(n, 60)
    for offset in range(max(0, onset_days - 2)):
        values = rains[offset : offset + 3]
        if any(value is None for value in values):
            earlier_gap = True
        elif sum(values, Decimal(0)) >= scenario.rain_onset_threshold_mm:
            onset = days[offset]
            break
    complete = valid_rain == valid_high == valid_low == n
    evidence_ids = sorted({eid for row in rows if row for eid in row.evidence_ids})
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "expected_days": n,
        "availability": "available"
        if complete
        else "partial"
        if any((valid_rain, valid_high, valid_low))
        else "missing",
        "coverage": {
            "rain_days": valid_rain,
            "tmax_days": valid_high,
            "tmin_days": valid_low,
            "rain_fraction": valid_rain / n,
            "tmax_fraction": valid_high / n,
            "tmin_fraction": valid_low / n,
        },
        "rain_mm": float(rainfall) if valid_rain == n else None,
        "observed_rain_subtotal_mm": float(rainfall) if valid_rain else None,
        "max_dry_spell_days": longest if valid_rain == n else None,
        "max_confirmed_dry_spell_days": longest if valid_rain else None,
        "dry_day_threshold_mm": float(scenario.dry_day_threshold_mm),
        "heat_threshold_c": 35,
        "heat_days": heat_count if valid_high == n else None,
        "observed_heat_days": heat_count if valid_high else None,
        "frost_threshold_c": 0,
        "frost_days": frost_count if valid_low == n else None,
        "observed_frost_days": frost_count if valid_low else None,
        "rain_onset": {
            "date": onset.isoformat() if onset and not earlier_gap else None,
            "first_observed_candidate_date": onset.isoformat() if onset else None,
            "availability": "partial"
            if earlier_gap
            else "available"
            if onset_days >= 3
            else "missing",
            "search_days": onset_days,
            "threshold_mm": float(scenario.rain_onset_threshold_mm),
            "consecutive_days": 3,
            "definition": "First 3-day sum meeting threshold in first 60 crop-window days; analysis definition, not a sowing recommendation.",
        },
        "evidence_ids": evidence_ids,
    }


def climate_exposure(
    rotation: Rotation,
    profile: PlotProfile,
    scenario: Scenario,
    *,
    planning_start: date | None = None,
) -> list[dict]:
    """Replay all crop windows in common historical calendar anchor years.

    February 29 maps to February 28 in a non-leap historical year. Actual
    historical calendar-day counts are returned; missing days are never zero rain.
    Requested years remain visible even if no observations exist.
    """
    original_year = (planning_start or rotation.start_date).year
    weather = {}
    duplicate_dates = set()
    for row in profile.weather:
        if row.date in weather:
            duplicate_dates.add(row.date)
        else:
            weather[row.date] = row
    years = (
        sorted(set(scenario.historical_years))
        if scenario.historical_years
        else sorted({d.year for d in weather})[-30:]
    )
    earliest_anchor = max(1, 1 - (rotation.start_date.year - original_year))
    latest_anchor = 9999 - (rotation.end_date.year - original_year)
    if any(year < earliest_anchor or year > latest_anchor for year in years):
        raise DomainError(
            "INVALID_HISTORICAL_YEAR", "Historical years must fit the full rotation calendar"
        )
    references = {ref.id: ref for ref in profile.evidence}
    origins = {
        references[eid].origin.value
        for row in profile.weather
        for eid in row.evidence_ids
        if eid in references
    }
    origin = (
        "modeled"
        if "modeled" in origins
        else "observed"
        if origins == {"observed"}
        else "assumption"
    )
    results = []
    for year in years:
        crop_windows = []
        for crop in rotation.crops:
            start = _mapped(crop.start_date, year, original_year)
            end = _mapped(crop.end_date, year, original_year)
            metrics = _window(start, end, weather, scenario)
            metrics.update(
                crop_instance_id=crop.id,
                crop_id=crop.crop_id,
                planning_start_date=crop.start_date.isoformat(),
                planning_end_date=crop.end_date.isoformat(),
            )
            crop_windows.append(metrics)
        start = _mapped(rotation.start_date, year, original_year)
        end = _mapped(rotation.end_date, year, original_year)
        total = _window(start, end, weather, scenario)
        results.append(
            {
                "id": f"historical-calendar-{year}",
                "anchor_year": year,
                "origin": origin,
                "method": "historical_calendar_replay",
                "synthetic": False,
                "availability": total["availability"],
                "rotation": total,
                "crops": crop_windows,
                "assumptions": [
                    "Historical calendar replay; not a forecast or calibrated yield model.",
                    "Temperature thresholds 35 C and 0 C are diagnostic definitions, not cultivar-specific damage thresholds.",
                    "February 29 is clamped to February 28 in non-leap historical years.",
                    "Missing precipitation interrupts confirmed dry runs and prevents complete-window totals.",
                ],
                "duplicate_weather_dates": sum(start <= day <= end for day in duplicate_dates),
                "yield_response": None,
                "evidence_ids": total["evidence_ids"],
            }
        )
    return results
