"""Environmental screening and historical thermal-time scheduling.

FAO equations and species envelopes are combined with explicitly versioned
planning policies. Scores are comparative indices, never yield probabilities.
No missing daily observation is replaced by dry weather or interpolated heat.
"""

from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from functools import lru_cache
from math import acos, cos, exp, isfinite, pi, radians, sin, sqrt, tan
from statistics import mean, median

from agrix.models import Availability, FarmConstraints, PlotProfile, Scenario

VERSION = "environmental-screening-2.0.0"
POLICY = "environmental-screening-policy-v2"
WEIGHTS = {
    "water": 0.35,
    "temperature": 0.20,
    "drought": 0.15,
    "soil_ph": 0.08,
    "soil_texture": 0.05,
    "soil_moisture": 0.07,
    "terrain": 0.08,
    "elevation": 0.02,
}


def bounded(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def envelope(value, limits):
    """Trapezoidal membership in a broad ecological range, not a lethal test."""
    low, optimum_low, optimum_high, high = limits
    if value < low or value > high:
        return 0.0
    if optimum_low <= value <= optimum_high:
        return 1.0
    if value < optimum_low:
        return bounded((value - low) / max(1e-9, optimum_low - low))
    return bounded((high - value) / max(1e-9, high - optimum_high))


def mapped(day, year, original_year):
    target = year + day.year - original_year
    return date(target, day.month, min(day.day, monthrange(target, day.month)[1]))


def extraterrestrial_radiation(day, latitude):
    """FAO56 eq. 21, MJ/m²/day."""
    latitude = radians(latitude)
    j = day.timetuple().tm_yday
    distance = 1 + 0.033 * cos(2 * pi * j / 365)
    declination = 0.409 * sin(2 * pi * j / 365 - 1.39)
    sunset = acos(bounded(-tan(latitude) * tan(declination), -1, 1))
    return (
        24
        * 60
        / pi
        * 0.0820
        * distance
        * (
            sunset * sin(latitude) * sin(declination)
            + cos(latitude) * cos(declination) * sin(sunset)
        )
    )


def hargreaves(day, latitude, tmin, tmax, tmean):
    """FAO56 eq. 52: convert radiation MJ/m²/day to equivalent mm with 0.408."""
    if latitude is None or tmin is None or tmax is None or tmean is None or tmax < tmin:
        return None
    radiation = extraterrestrial_radiation(day, latitude)
    return max(0.0, 0.0023 * (tmean + 17.8) * sqrt(tmax - tmin) * radiation * 0.408)


def coefficient(params, fraction):
    """Four stages (20/30/30/20%): explicit uncalibrated stage allocation."""
    initial, middle, end = params.kc
    if fraction < 0.2:
        return initial
    if fraction < 0.5:
        return initial + (middle - initial) * (fraction - 0.2) / 0.3
    if fraction < 0.8:
        return middle
    return middle + (end - middle) * min(1, (fraction - 0.8) / 0.2)


def penman_monteith(day, latitude, tmin, tmax, tmean, humidity, wind, solar, elevation=0):
    """FAO56 daily reference ET, using measured mean RH (eq. 19 approximation)."""
    if any(value is None for value in (latitude, tmin, tmax, tmean, humidity, wind, solar)):
        return None
    if not 0 <= humidity <= 100 or wind < 0 or solar < 0 or tmax < tmin:
        return None
    ra = extraterrestrial_radiation(day, latitude)

    def saturation(temperature):
        return 0.6108 * exp(17.27 * temperature / (temperature + 237.3))

    es = (saturation(tmin) + saturation(tmax)) / 2
    ea = es * humidity / 100
    delta = 4098 * saturation(tmean) / (tmean + 237.3) ** 2
    pressure = 101.3 * ((293 - 0.0065 * elevation) / 293) ** 5.26
    gamma = 0.000665 * pressure
    clear_sky = (0.75 + 2e-5 * elevation) * ra
    cloud = 1.35 * bounded(solar / max(1e-9, clear_sky), 0.3, 1) - 0.35
    longwave = (
        4.903e-9
        * ((tmax + 273.16) ** 4 + (tmin + 273.16) ** 4)
        / 2
        * (0.34 - 0.14 * sqrt(ea))
        * cloud
    )
    net_radiation = 0.77 * solar - longwave
    wind = max(0.5, wind)
    return max(
        0,
        (0.408 * delta * net_radiation + gamma * 900 / (tmean + 273) * wind * (es - ea))
        / (delta + gamma * (1 + 0.34 * wind)),
    )


class EnvironmentalModel:
    def __init__(self, profile: PlotProfile, constraints, planning_start, scenario=None, plot=None):
        self.profile = profile
        self.constraints = constraints or FarmConstraints()
        self.planning_start = planning_start
        self.scenario = scenario or Scenario()
        self.references = {e.id: e for e in profile.evidence}
        self.daily_theta = {
            row.date: float(row.root_zone_moisture)
            for row in profile.weather
            if row.root_zone_moisture is not None
        }
        self.weather = {}
        for row in profile.weather:
            if row.date in self.weather:
                continue
            low = float(row.tmin_c) if row.tmin_c is not None else None
            high = float(row.tmax_c) if row.tmax_c is not None else None
            average = (
                float(row.tmean_c)
                if row.tmean_c is not None
                else ((low + high) / 2 if low is not None and high is not None else None)
            )
            self.weather[row.date] = (
                float(row.rain_mm) if row.rain_mm is not None and row.rain_mm >= 0 else None,
                low,
                high,
                average,
            )
        self.years = sorted(set(self.scenario.historical_years or [d.year for d in self.weather]))[
            -30:
        ]
        self.latitude = None
        if plot is not None:
            from shapely.geometry import shape

            self.latitude = shape(plot.geometry.model_dump()).centroid.y
        else:
            bounds = [e.geographic_bounds for e in profile.evidence if e.geographic_bounds]
            if bounds:
                self.latitude = mean((b[1] + b[3]) / 2 for b in bounds)
        self.texture, self.hydraulics = self._texture()
        self.slope = self.metric("terrain_slope", {"degrees"}, 0, 90)
        # Runoff is a disclosed slope sensitivity, not a calibrated runoff model.
        self.runoff = min(0.45, 0.5 * tan(radians(self.slope))) if self.slope is not None else 0
        self.weather_evidence = sorted({eid for r in profile.weather for eid in r.evidence_ids})
        self.et0, self.et_methods = {}, set()
        elevation = self.metric("elevation", {"m"}, -500, 9000)
        original = {row.date: row for row in reversed(profile.weather)}
        for day, (_, low, high, average) in self.weather.items():
            row = original[day]
            pm = penman_monteith(
                day,
                self.latitude,
                low,
                high,
                average,
                float(row.humidity_pct) if row.humidity_pct is not None else None,
                float(row.wind_m_s) if row.wind_m_s is not None else None,
                float(row.solar_mj_m2) if row.solar_mj_m2 is not None else None,
                elevation or 0,
            )
            self.et0[day] = (
                pm if pm is not None else hargreaves(day, self.latitude, low, high, average)
            )
            if self.et0[day] is not None:
                self.et_methods.add(
                    "fao56_penman_monteith_mean_rh"
                    if pm is not None
                    else "hargreaves_temperature_fallback"
                )
        # Per-evaluation caches cannot retain other users' profiles after a request.
        self._duration = lru_cache(maxsize=4096)(self._duration)
        self.crop_assessment = lru_cache(maxsize=8192)(self.crop_assessment)

    def metric(self, key, units, minimum=None, maximum=None):
        tests = [
            test
            for test in self.constraints.soil_tests
            if test.property == key
            and test.unit in units
            and (key != "soil_moisture" or (test.depth_top_cm, test.depth_bottom_cm) == (0, 100))
        ]
        if tests:
            return float(max(tests, key=lambda test: test.sample_date).value)
        item = self.profile.metrics.get(key)
        if (
            item is None
            or item.value is None
            or item.availability not in (Availability.available, Availability.partial)
            or item.unit not in units
        ):
            return None
        value = float(item.value)
        if (
            not isfinite(value)
            or (minimum is not None and value < minimum)
            or (maximum is not None and value > maximum)
        ):
            return None
        return value

    def ids(self, *keys):
        if any(test.property in keys for test in self.constraints.soil_tests):
            return ["farmer-soil-input-v2"]
        return sorted(
            {
                eid
                for key in keys
                for eid in (
                    self.profile.metrics[key].evidence_ids if key in self.profile.metrics else []
                )
            }
        )

    def _texture(self):
        sand = self.metric("sand_pct", {"%"}, 0, 100)
        clay = self.metric("clay_pct", {"%"}, 0, 100)
        silt = self.metric("silt_pct", {"%"}, 0, 100)
        if sand is None or clay is None or sand + clay > 100.01:
            return None, None
        if silt is not None and abs(sand + clay + silt - 100) > 5:
            return None, None
        texture = "heavy" if clay >= 35 else "light" if sand >= 65 and clay < 20 else "medium"
        # FAO56 example 36 representative values; mapping broad texture to them is a policy assumption.
        return texture, {"light": (0.15, 0.06), "medium": (0.32, 0.15), "heavy": (0.35, 0.23)}[
            texture
        ]

    def _theta(self, day=None):
        tests = [
            test
            for test in self.constraints.soil_tests
            if test.property == "soil_moisture"
            and test.unit in ("m3/m3", "%")
            and (test.depth_top_cm, test.depth_bottom_cm) == (0, 100)
            and (day is None or test.sample_date == day)
        ]
        if tests:
            test = max(tests, key=lambda value: value.sample_date)
            return float(test.value) * (0.01 if test.unit == "%" else 1)
        if day is not None and day in self.daily_theta:
            return self.daily_theta[day]
        item = self.profile.metrics.get("soil_moisture")
        theta = self.metric("soil_moisture", {"m3/m3"}, 0, 1)
        if theta is None or item.depth_cm != (0, 100):
            return None
        periods = [self.references[e].period for e in item.evidence_ids if e in self.references]
        if day is not None and not any(p and p.start == p.end == day for p in periods):
            return None  # A seasonal mean is context, never a sowing-day initial state.
        return theta

    def estimate_duration(self, crop, start):
        params = crop.environmental_parameters
        if crop.fallow or params is None:
            return {
                "method": "nominal_calendar",
                "duration_days": crop.duration_days,
                "availability": "missing",
                "calibrated": False,
            }
        return self._duration(crop.id, start, crop.duration_days, params.model_dump_json())

    def _duration(self, crop_id, start, nominal, parameters):
        from agrix.models import EnvironmentalParameters

        params = EnvironmentalParameters.model_validate_json(parameters)
        durations, early, late, histories = [], [], [], []
        for year in self.years:
            first = mapped(start, year, self.planning_start.year)
            accumulated, reached = 0.0, {}
            missing = False
            targets = [
                params.thermal_target_range[0],
                params.thermal_target_gdd,
                params.thermal_target_range[1],
            ]
            for offset in range(params.cycle_days[1]):
                row = self.weather.get(first + timedelta(days=offset))
                if row is None or row[3] is None:
                    missing = True
                    break
                accumulated += max(0, min(row[3], params.thermal_upper_c) - params.thermal_base_c)
                for index, target in enumerate(targets):
                    if accumulated >= target and offset + 1 >= params.cycle_days[0]:
                        reached.setdefault(index, offset + 1)
                if len(reached) == 3:
                    break
            if 1 in reached:
                durations.append(reached[1])
            if 0 in reached:
                early.append(reached[0])
            if 2 in reached:
                late.append(reached[2])
            histories.append(
                {
                    "anchor_year": year,
                    "duration_days": reached.get(1),
                    "missing_weather": missing,
                    "target_reached": 1 in reached,
                    "accumulated_gdd": round(accumulated, 2),
                }
            )
        cold_censored = any(not h["missing_weather"] and not h["target_reached"] for h in histories)
        chosen = (
            params.cycle_days[1]
            if cold_censored
            else int(median(durations))
            if durations
            else nominal
        )
        complete_range = len(early) == len(late) == len(self.years) and bool(self.years)
        return {
            "method": "historical_thermal_target_unreached"
            if cold_censored
            else "historical_thermal_time"
            if durations
            else "nominal_fallback_missing_thermal_evidence",
            "duration_days": chosen,
            "earliest_days": min(early) if early else None,
            "latest_days": max(late) if complete_range else None,
            "range_censored": not complete_range,
            "maturity_unreached_in_complete_year": cold_censored,
            "thermal_target_gdd": params.thermal_target_gdd,
            "thermal_target_range": list(params.thermal_target_range),
            "base_c": params.thermal_base_c,
            "upper_c": params.thermal_upper_c,
            "historical_years": histories,
            "complete_maturity_years": len(durations),
            "availability": "partial" if durations else "missing",
            "calibrated": False,
            "evidence_ids": sorted(set(self.weather_evidence + params.source_ids)),
            "assumption": "Thermal targets are provisional; cultivar, vernalization and photoperiod are uncalibrated. Missing days stop accumulation; no growth is imputed.",
        }

    def _component(self, key, score, coverage=1.0, evidence=None, **values):
        return {
            "id": key,
            "score": round(100 * bounded(score), 3) if score is not None else None,
            "coverage": round(coverage if score is not None else 0, 5),
            "weight": WEIGHTS[key],
            "evidence_ids": sorted(set(evidence or [])),
            "values": values,
        }

    def _moisture_score(self, params):
        theta = self._theta()
        if theta is None or self.hydraulics is None:
            return None
        fc, wp = self.hydraulics
        relative = bounded((theta - wp) / (fc - wp))
        return bounded(relative / (1 - params.depletion_fraction))

    def crop_assessment(self, crop_id, start, end, parameters):
        from agrix.models import EnvironmentalParameters

        params = EnvironmentalParameters.model_validate_json(parameters)
        temperatures, droughts, establishment, dry_lengths = [], [], [], []
        valid_t = valid_r = expected = 0
        for year in self.years:
            first = mapped(start, year, self.planning_start.year)
            last = mapped(end, year, self.planning_start.year)
            count = (last - first).days + 1
            dry = longest = year_rain = 0
            initial_rain = initial_demand = 0.0
            initial_known = 0
            for offset in range(count):
                day = first + timedelta(days=offset)
                row = self.weather.get(day, (None, None, None, None))
                rain, low, high, average = row
                expected += 1
                if average is not None:
                    comfort = envelope(average, params.temperature_c)
                    if high is not None:
                        comfort *= 0.7 + 0.3 * envelope(high, params.temperature_c)
                    if low is not None and low < 0:
                        comfort *= 0.8  # Diagnostic cold exposure, not a lethal threshold.
                    temperatures.append(comfort)
                    valid_t += 1
                if rain is None:
                    dry = 0
                else:
                    valid_r += 1
                    year_rain += 1
                    dry = dry + 1 if rain < float(self.scenario.dry_day_threshold_mm) else 0
                    longest = max(longest, dry)
                et = self.et0.get(day)
                if offset < 21 and rain is not None and et is not None:
                    initial_rain += rain * (1 - self.runoff)
                    initial_demand += et * params.kc[0]
                    initial_known += 1
            if initial_known:
                establishment.append(bounded(initial_rain / max(1, initial_demand)))
            if count and year_rain:
                dry_lengths.append(longest)
                # Uncalibrated spell-tolerance index; storage/rooting/depletion change its scale.
                reserve = (
                    1000
                    * (self.hydraulics[0] - self.hydraulics[1])
                    * min(1, params.root_depth_m)
                    * params.depletion_fraction
                    if self.hydraulics
                    else 30 * params.depletion_fraction / 0.5
                )
                droughts.append(exp(-longest / max(7, reserve / 2)))
        ph = self.metric("soil_ph", {"pH", "1"}, 0, 14)
        elevation = self.metric("elevation", {"m"}, -500, 9000)
        components = [
            self._component(
                "temperature",
                mean(temperatures) if temperatures else None,
                valid_t / expected if expected else 0,
                self.weather_evidence,
                ecological_range_c=list(params.temperature_c),
            ),
            self._component(
                "drought",
                mean(droughts) if valid_r else None,
                valid_r / expected if expected else 0,
                self.weather_evidence,
                max_confirmed_dry_spell_days=max(dry_lengths) if valid_r else None,
                full_window_verified=valid_r == expected and bool(expected),
            ),
            self._component(
                "soil_ph",
                envelope(ph, params.ph) if ph is not None else None,
                evidence=self.ids("soil_ph"),
                ph=ph,
            ),
            self._component(
                "soil_texture",
                (1 if self.texture in params.preferred_texture else 0.5) if self.texture else None,
                evidence=self.ids("sand_pct", "clay_pct", "silt_pct"),
                texture=self.texture,
            ),
            self._component(
                "soil_moisture",
                self._moisture_score(params),
                evidence=self.ids("soil_moisture", "sand_pct", "clay_pct"),
                role="historical_root_zone_context; not future sowing-day moisture",
            ),
            self._component(
                "elevation",
                (1 if elevation <= params.altitude_max_m else 0)
                if elevation is not None and params.altitude_max_m
                else None,
                evidence=self.ids("elevation"),
                elevation_m=elevation,
            ),
        ]
        return {
            "crop_id": crop_id,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "components": components,
            "establishment_score": mean(establishment) if establishment else None,
        }

    def _water(self, selections, crops):
        """Shared 0-1 m storage across crops and fallow; initial depletion is bracketed.

        Complete daily weather is required to report a season total. Following a
        gap storage is bracketed again; missing precipitation is never dry rain.
        """
        output = defaultdict(
            lambda: {
                "demand": 0.0,
                "actual_low": 0.0,
                "actual_high": 0.0,
                "rain": 0.0,
                "known": 0,
                "expected": 0,
            }
        )
        if not selections:
            return output
        capacity = 1000 * (self.hydraulics[0] - self.hydraulics[1]) if self.hydraulics else None
        first, last = selections[0].start_date, selections[-1].end_date
        for year in self.years:
            storage_low, storage_high = 0.0, capacity or 0.0
            cursor = 0
            historical_first = mapped(first, year, self.planning_start.year)
            historical_last = mapped(last, year, self.planning_start.year)
            for offset in range((historical_last - historical_first).days + 1):
                actual_day = historical_first + timedelta(days=offset)
                planned = mapped(actual_day, self.planning_start.year, year)
                while cursor + 1 < len(selections) and selections[cursor].end_date < planned:
                    cursor += 1
                selected = selections[cursor]
                crop = crops[selected.crop_id]
                active = selected.start_date <= planned <= selected.end_date and not crop.fallow
                params = crop.environmental_parameters if active else None
                rain, low, high, average = self.weather.get(actual_day, (None, None, None, None))
                et = self.et0.get(actual_day)
                stats = output[cursor]
                if active:
                    stats["expected"] += 1
                if rain is None or et is None:
                    storage_low, storage_high = 0.0, capacity or 0.0
                    continue
                theta = self._theta(actual_day) if offset == 0 else None
                if capacity and theta is not None:
                    storage_low = storage_high = (
                        bounded(theta - self.hydraulics[1], 0, capacity / 1000) * 1000
                    )
                irrigation = float(self.constraints.irrigation_mm_by_date.get(planned, 0))
                effective = rain * (1 - self.runoff) + irrigation
                fraction = (planned - selected.start_date).days / max(
                    1, (selected.end_date - selected.start_date).days
                )
                demand = et * (coefficient(params, fraction) if params else 0.2)
                totals = []
                for storage in (storage_low, storage_high):
                    if capacity is None:
                        actual = min(effective, demand)
                        next_storage = 0.0
                    else:
                        storage = min(capacity, storage + effective)
                        root_fraction = min(1.0, params.root_depth_m) if params else 0.25
                        accessible = storage * root_fraction
                        root_capacity = capacity * root_fraction
                        p = params.depletion_fraction if params else 0.5
                        ks = min(1.0, accessible / max(1e-9, (1 - p) * root_capacity))
                        actual = min(accessible, demand * ks)
                        next_storage = max(0, storage - actual)
                    totals.append((next_storage, actual))
                storage_low, storage_high = totals[0][0], totals[1][0]
                if active:
                    # Mid-season deficits carry extra ranking weight, explicitly a policy.
                    weight = 1.5 if 0.5 <= fraction <= 0.8 else 1
                    stats["demand"] += demand * weight
                    stats["actual_low"] += totals[0][1] * weight
                    stats["actual_high"] += totals[1][1] * weight
                    stats["rain"] += effective
                    stats["known"] += 1
        return output

    def assess(self, selections, catalog):
        crops = {c.id: c for c in catalog.crops}
        productive = [(i, s) for i, s in enumerate(selections) if not crops[s.crop_id].fallow]
        water = self._water(selections, crops)
        assessments = []
        for index, selected in productive:
            crop = crops[selected.crop_id]
            params = crop.environmental_parameters
            if params is None:
                continue
            data = self.crop_assessment(
                crop.id, selected.start_date, selected.end_date, params.model_dump_json()
            )
            # Cached observations are immutable; attach sequence-dependent water in a new list.
            data = {**data, "components": list(data["components"])}
            stats = water[index]
            score = stats["actual_low"] / stats["demand"] if stats["demand"] else None
            high_score = stats["actual_high"] / stats["demand"] if stats["demand"] else None
            data["components"].append(
                self._component(
                    "water",
                    score,
                    stats["known"] / stats["expected"] if stats["expected"] else 0,
                    [
                        *self.weather_evidence,
                        *self.ids("soil_moisture", "sand_pct", "clay_pct", "terrain_slope"),
                    ],
                    optimistic_score=round(100 * high_score, 3) if high_score is not None else None,
                    method="bracketed_daily_root_zone_balance"
                    if self.hydraulics
                    else "rainfall_demand_screen_no_soil_storage",
                    root_depth_modeled_m=min(1, params.root_depth_m),
                    effective_rain_subtotal_mm=round(stats["rain"], 2) if stats["known"] else None,
                    stress_weighted_demand_subtotal_mm=round(stats["demand"], 2)
                    if stats["known"]
                    else None,
                    complete_window=stats["known"] == stats["expected"] and bool(stats["expected"]),
                    irrigation_supply_known=self.constraints.production_system == "rainfed",
                    slope_runoff_fraction=self.runoff if self.slope is not None else None,
                )
            )
            data["timing"] = self.estimate_duration(crop, selected.start_date)
            assessments.append(data)
        all_days = sum((s.end_date - s.start_date).days + 1 for s in selections)
        active_days = sum((s.end_date - s.start_date).days + 1 for _, s in productive)
        # Crop cover fraction is a disclosed generic assumption; no future NDVI is invented.
        bare_fraction = 1 - 0.65 * active_days / max(1, all_days)
        terrain = self._component(
            "terrain",
            1 - min(1, tan(radians(self.slope)) / tan(radians(30))) * bare_fraction
            if self.slope is not None
            else None,
            evidence=self.ids("terrain_slope"),
            slope_degrees=self.slope,
            bare_ground_exposure_fraction=round(bare_fraction, 4),
            method="slope_and_calendar_cover_screen; not predicted erosion or machinery safety",
        )
        combined = []
        for key in WEIGHTS:
            parts = [c for item in assessments for c in item["components"] if c["id"] == key]
            if key == "terrain":
                combined.append(terrain)
                continue
            known = [p for p in parts if p["score"] is not None]
            combined.append(
                {
                    "id": key,
                    "weight": WEIGHTS[key],
                    "score": round(mean(p["score"] for p in known), 3) if known else None,
                    "coverage": round(sum(p["coverage"] for p in known) / max(1, len(parts)), 5),
                    "evidence_ids": sorted({eid for p in parts for eid in p["evidence_ids"]}),
                }
            )
        known_weight = sum(c["weight"] * c["coverage"] for c in combined if c["score"] is not None)
        lower = sum(
            c["weight"] * c["coverage"] * c["score"] for c in combined if c["score"] is not None
        )
        storage_uncertainty = (
            mean(
                max(0, c["values"].get("optimistic_score", c["score"]) - c["score"])
                for data in assessments
                for c in data["components"]
                if c["id"] == "water" and c["score"] is not None
            )
            if any(
                c["id"] == "water" and c["score"] is not None
                for data in assessments
                for c in data["components"]
            )
            else 0
        )
        water_component = next(c for c in combined if c["id"] == "water")
        upper = min(
            100,
            lower
            + (1 - known_weight) * 100
            + WEIGHTS["water"] * water_component["coverage"] * storage_uncertainty,
        )
        return {
            "version": VERSION,
            "score": round(lower / known_weight, 3) if known_weight else None,
            "score_range": [round(lower, 3), round(upper, 3)],
            "ranking_score": round(lower, 3) if known_weight else None,
            "coverage": round(known_weight, 5),
            "calibrated": False,
            "confidence": "limited" if known_weight >= 0.5 else "insufficient",
            "components": combined,
            "crops": assessments,
            "historical_anchor_years": self.years,
            "water_availability": {
                **self.profile.water_availability,
                "irrigation_source_label": self.constraints.irrigation_source_label,
                "irrigation_verified_on": str(self.constraints.irrigation_verified_on)
                if self.constraints.irrigation_verified_on
                else None,
                "scheduled_irrigation_mm": str(
                    sum(self.constraints.irrigation_mm_by_date.values())
                ),
                "basis": "NASA rain + dated SMAP root-zone initial water + soil storage + explicit irrigation schedule; no inferred reservoir/well entitlement",
            },
            "evapotranspiration_methods": sorted(self.et_methods),
            "data_usage": self.data_usage(),
            "ranking_basis": "Conservative environmental score bound first; economics only breaks environmental ties.",
            "assumptions": [
                "Historical screening, not a weather forecast, yield prediction or suitability probability.",
                "Thermal targets and stage fractions require local cultivar calibration; no vernalization or photoperiod response.",
                "FAO56 Hargreaves ET0 and generic Kc; no verified irrigation amount, groundwater or capillary rise.",
                "FAO representative soil hydraulics selected by broad texture; initial storage bracketed empty to full.",
                "Daily storage is carried across crops and fallow; missing days reopen its uncertainty interval.",
                "Slope runoff, calendar cover, dry-spell index and score weights are explicit policy assumptions.",
                "Unknown components widen the score interval and do not become observed zero suitability.",
            ],
            "evidence_ids": sorted(
                {
                    POLICY,
                    *[eid for c in combined for eid in c["evidence_ids"]],
                    *[
                        eid
                        for _, s in productive
                        for eid in (
                            crops[s.crop_id].environmental_parameters.source_ids
                            if crops[s.crop_id].environmental_parameters
                            else []
                        )
                    ],
                }
            ),
        }

    def data_usage(self):
        roles = {
            "soil_ph": "crop-specific ecological pH and local guide checks",
            "sand_pct": "texture suitability and approximate soil water storage",
            "silt_pct": "texture consistency check",
            "clay_pct": "texture suitability and approximate soil water storage",
            "soil_moisture": "dated root-zone initialization when exact date/depth match; otherwise historical moisture context",
            "terrain_slope": "runoff sensitivity and calendar bare-ground exposure",
            "elevation": "species elevation envelope where published; weather already includes local temperature",
            "ndvi": "vegetation context only; no validated crop-identity or future-cover relationship",
            "soil_moisture_surface": "surface context only; cannot initialize the 0-100 cm reservoir",
            "rainfall_mm": "daily precipitation is used instead of a period-wide total",
            "temperature_c": "daily temperatures are used instead of a period-wide mean",
        }
        return [
            {
                "metric": key,
                "availability": value.availability.value,
                "role": roles.get(
                    key, "retained as context; no validated decision relationship encoded"
                ),
                "evidence_ids": value.evidence_ids,
            }
            for key, value in sorted(self.profile.metrics.items())
        ]


def environmental_sort_key(assessment):
    score = assessment.get("ranking_score") if assessment else None
    return (score is None, -(score or 0), -(assessment or {}).get("coverage", 0))
