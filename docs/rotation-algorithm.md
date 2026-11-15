# Ghayth rotation algorithm

Implementation reference, reviewed 2026-11-15. This document describes the current code, including its search limits, fallback behavior and presentation rules.

The live algorithm is a bounded search over crop sequences and dates, followed by historical environmental assessment and a shared feasibility evaluator. It runs independently of conversational or speech models. Environmental suitability determines candidate generation; economics supplies supporting estimates and secondary ranking.

Current identifiers:

| Item | Identifier |
| --- | --- |
| Environmental model | `environmental-screening-2.0.0` |
| Screening policy | `environmental-screening-policy-v2` |
| Live catalog | `morocco-environment-screening-v2` |

## 1. Implementation map

Paths are relative to the repository root.

| File | Responsibility |
| --- | --- |
| [service.py](../apps/api/src/agrix/service.py) | `screening_profile`, `suggest`, `evaluate`: orchestration, context, economic enrichment and API ordering |
| [planning.py](../apps/api/src/agrix/agronomy/planning.py) | Sowing windows, candidate search, predecessor rules and agronomic checks |
| [environment.py](../apps/api/src/agrix/agronomy/environment.py) | Weather preparation, thermal duration, ET0, water balance and environmental scores |
| [catalog.py](../apps/api/src/agrix/agronomy/catalog.py) | Versioned crop parameters and geographic rule coverage |
| [evaluator.py](../apps/api/src/agrix/evaluator.py) | Shared rotation construction, feasibility checks, status and snapshots |
| [climate.py](../apps/api/src/agrix/agronomy/climate.py) | Historical exposure diagnostics |
| [economics.py](../apps/api/src/agrix/economics.py) | Automatic Moroccan economic references |
| [finance.py](../apps/api/src/agrix/finance.py) | Operations, labor, machinery and dated cash flow |
| [models.py](../apps/api/src/agrix/models.py) | Validated request, parameter and result contracts |
| [App.tsx](../apps/web/src/App.tsx) | Farmer requests, duration changes, displayed ordering and three-card limit |

The scientific acquisition/extraction pipeline is documented separately in [data setup](data-setup.md) and [Earth integration](earth-integration.md). This algorithm consumes its resulting profile.

## 2. Inputs and output

### 2.1 Request

`POST /rotations/suggest` accepts:

| Input | Meaning |
| --- | --- |
| `plot_id`, `plot_revision` | Identify the current field and its revision |
| `start_date` | Earliest planning date |
| `horizon_days` | Inclusive planning length; default 730, API range 1–3650 days |
| `constraints` | Farming system, irrigation, previous crops, machinery and optional detailed farm inputs |
| `scenario` | Historical years, dry-day threshold and economic sensitivity assumptions |

The planning interval is:

```text
planning_end = start_date + horizon_days - 1 day
```

The UI offers 1, 2 or 3 years as 365, 730 or 1095 days. These are fixed day counts, not calendar-year anniversaries.

### 2.2 Field profile

The profile supplies daily rainfall, minimum/maximum/mean temperature, optional humidity/wind/radiation, dated root-zone moisture, soil properties, slope, elevation, rule coverage and evidence.

Units used by the environmental model include mm/day, °C, m³/m³, percent sand/silt/clay, pH, degrees of slope and meters of elevation. Field area is hectares. A regional grid remains regional evidence.

### 2.3 Crop definitions

Each productive crop has a family, supported regions/systems, sowing windows, nominal duration, environmental parameters, predecessor restrictions and operation templates. The live crops are wheat, durum wheat, barley, faba bean, lentil and winter chickpea. Fallow is a separate explicit catalog entry.

The authoritative definitions are [morocco_catalog.yaml](../apps/api/src/agrix/fixtures/morocco_catalog.yaml) and [environmental_rules.yaml](../apps/api/src/agrix/fixtures/environmental_rules.yaml). Appendix A lists the current numerical parameters.

### 2.4 Output

The generator returns up to six candidate evaluation requests. Each is passed through the shared evaluator. The API returns evaluated candidates with dates, environmental components, evidence coverage, checks, status, operations, financial estimates and frozen input snapshots. The UI displays up to three distinct candidates.

## 3. Validate context and determine rule coverage

1. Load the project-scoped plot and reject a stale requested revision.
2. Load the applicable catalog and the plot's analysis/profile.
3. Work on a copy of the profile; preserve stored observations and saved plans.
4. Determine the applicable rule region using the whole polygon.

Named pilot lookup boxes cover approximate Chaouia/Settat, Saiss/Meknes, Zaer and Abda extents. A box must cover the entire polygon. If multiple boxes qualify, choose the smallest extent, then zone ID as a deterministic tie-break. Approximate matches remain partial. No centroid or nearest-region assignment establishes local rule coverage.

For `unknown` or `irrigated` farming systems, region lookup can temporarily use rainfed reference constraints. The declared system is retained in the real request, and an explicit conditional-screening evidence record is added. This supplies qualified reference dates without inventing irrigation amounts.

If no named region resolves under the rainfed reference, the service uses `morocco_screening` with partial coverage. The environmental catalog adds this zone to productive crop eligibility. Region-specific sowing windows still apply: broad screening does not unlock a regional chickpea calendar outside its named zones.

## 4. Prepare historical environmental data

### 4.1 Daily records and historical anchors

The weather lookup keeps the first weather record for each date. Negative rainfall is treated as missing. Mean temperature uses the supplied daily mean, or `(Tmin + Tmax) / 2` when both extremes exist.

Historical anchor years are the requested scenario years, or the calendar years represented in weather. The environmental model sorts/deduplicates and keeps at most the latest 30 anchors. Requested/replayed windows can have incomplete evidence; they remain visible through coverage rather than receiving invented weather.

Map a planned date into an anchor year with:

```text
historical_year = anchor_year + planned_date.year - planning_start.year
historical_month = planned_date.month
historical_day = min(planned_date.day, last_day_of_historical_month)
```

This preserves offsets between years of a rotation. February 29 maps to February 28 where needed. The water balance walks actual historical calendar days, including historical leap days.

### 4.2 Latitude and observations

Latitude comes from the plot centroid for weather calculations. If no plot is passed, the model can use mean evidence-bounds latitude. This computational latitude is separate from geographic crop-rule matching.

Supported field soil tests override the corresponding profile property; the newest matching test is selected. Dated root-zone initialization requires a matching day and the full 0–100 cm depth. A surface reading or a seasonal mean cannot initialize an arbitrary historical sowing date.

### 4.3 Texture and representative hydraulics

Sand and clay must be known percentages, their sum must not exceed 100.01, and supplied silt must make the three fractions sum within 5 percentage points of 100.

```text
heavy  if clay >= 35%
light  if sand >= 65% and clay < 20%
medium otherwise
```

| Texture | Field capacity, FC | Wilting point, WP |
| --- | ---: | ---: |
| Light | 0.15 | 0.06 |
| Medium | 0.32 | 0.15 |
| Heavy | 0.35 | 0.23 |

FC/WP are volumetric fractions. This broad mapping is an explicit approximation, not measured field hydraulics. Unknown/inconsistent texture leaves storage capacity unknown.

### 4.4 Runoff sensitivity

For known slope:

```text
runoff_fraction = min(0.45, 0.5 × tan(slope_radians))
```

Unknown slope uses zero runoff in the numerical balance while the terrain component remains missing. This is a documented implementation assumption, not confirmation of zero actual runoff.

## 5. Calculate daily reference evapotranspiration

The model first tries FAO56 Penman–Monteith, then a temperature-based Hargreaves fallback. If neither has the required inputs, ET0 is missing.

### 5.1 Shared extraterrestrial radiation

For latitude `φ` in radians and day-of-year `J`:

```text
dr = 1 + 0.033 × cos(2πJ / 365)
δ  = 0.409 × sin(2πJ / 365 - 1.39)
ωs = acos(clamp(-tan(φ) × tan(δ), -1, 1))

Ra = (24 × 60 / π) × 0.0820 × dr
     × [ωs × sin(φ) × sin(δ) + cos(φ) × cos(δ) × sin(ωs)]
```

`Ra` is MJ/m²/day. `clamp(x,a,b)` bounds a value to the closed interval.

### 5.2 Penman–Monteith

Requires latitude, Tmin, Tmax, Tmean, relative humidity, wind and solar radiation. Reject invalid humidity, negative wind/radiation or `Tmax < Tmin`. Missing elevation uses zero in the pressure calculation.

The implemented daily equation is:

```text
e_sat(T) = 0.6108 × exp(17.27T / (T + 237.3))
es = [e_sat(Tmin) + e_sat(Tmax)] / 2
ea = es × RH / 100
Δ  = 4098 × e_sat(Tmean) / (Tmean + 237.3)²
P  = 101.3 × [(293 - 0.0065 × elevation) / 293]^5.26
γ  = 0.000665 × P
Rso = (0.75 + 0.00002 × elevation) × Ra
cloud_factor = 1.35 × clamp(Rs / max(1e-9, Rso), 0.3, 1) - 0.35
Rnl = 4.903e-9 × [(Tmax + 273.16)^4 + (Tmin + 273.16)^4] / 2
      × (0.34 - 0.14 × sqrt(ea)) × cloud_factor
Rn = 0.77 × Rs - Rnl
u = max(0.5, wind_m_s)

ET0 = max(0,
  [0.408 × Δ × Rn + γ × 900/(Tmean + 273) × u × (es - ea)]
  / [Δ + γ × (1 + 0.34 × u)]
)
```

The implementation uses mean RH to approximate actual vapor pressure and records method `fao56_penman_monteith_mean_rh`.

### 5.3 Hargreaves fallback

Requires latitude and valid Tmin, Tmax and Tmean:

```text
ET0 = max(0, 0.0023 × (Tmean + 17.8) × sqrt(Tmax - Tmin) × Ra × 0.408)
```

The 0.408 factor converts radiation to equivalent evaporation. The recorded method is `hargreaves_temperature_fallback`. ET0 is in mm/day.

## 6. Estimate crop duration from thermal accumulation

For each crop, proposed sowing date and historical anchor:

1. Map sowing into the anchor year.
2. Walk forward up to the crop's maximum cycle length.
3. Stop accumulation at the first missing daily mean temperature.
4. Add daily growing-degree accumulation:

   ```text
   daily_GDD = max(0, min(Tmean, thermal_upper_c) - thermal_base_c)
   ```

5. Record when cumulative GDD first reaches each of the low, central and high targets, provided the minimum cycle length has elapsed.
6. Stop early once all three targets have been reached.

Choose the duration as follows:

```text
if any complete simulated year fails to reach the central target:
    duration = crop maximum cycle length
    method = historical_thermal_target_unreached
else if at least one central target was reached:
    duration = integer-truncated median of central-target durations
    method = historical_thermal_time
else:
    duration = catalog nominal duration
    method = nominal_fallback_missing_thermal_evidence
```

The earliest sensitivity is the minimum low-target duration available. The latest sensitivity is returned only if both low and high targets were reached in every anchor. Otherwise the range is censored. Evidence, per-year GDD, missing-weather flags and target-reached flags are retained.

For fallow or a crop without environmental parameters, duration estimation returns the nominal calendar duration. Explicit farmer end dates take precedence during shared evaluation.

## 7. Assess an individual crop/date window

Scores are built as fractions from 0 to 1, clamped, then exposed as 0–100 values. Each component carries coverage and evidence. These calculations are reused during search and full assessment.

### 7.1 Ecological envelope

Temperature and pH use a trapezoid defined by `[low, optimum_low, optimum_high, high]`:

```text
0                                   outside [low, high]
1                                   inside [optimum_low, optimum_high]
(x - low) / (optimum_low - low)      below the optimum interval
(high - x) / (high - optimum_high)  above the optimum interval
```

The code protects denominators with `max(1e-9, denominator)` and clamps to 0–1. An ecological envelope is not a cultivar-specific lethal threshold.

### 7.2 Temperature

For each observed daily mean in each replayed crop window:

```text
comfort = envelope(Tmean, crop_temperature_limits)
if Tmax exists:
    comfort *= 0.7 + 0.3 × envelope(Tmax, crop_temperature_limits)
if Tmin exists and Tmin < 0°C:
    comfort *= 0.8
```

Temperature score is the mean comfort over valid daily means. Coverage is valid daily means divided by all expected replay days.

### 7.3 Drought exposure

A dry day has rainfall below `scenario.dry_day_threshold_mm`, default 1 mm. Missing rain resets the confirmed dry-run counter; it is not a dry day.

For each anchor with rain observations, find its longest confirmed dry run, `L`:

```text
reserve = 1000 × (FC - WP) × min(1, root_depth_m) × depletion_fraction
          when hydraulics are known

reserve = 30 × depletion_fraction / 0.5
          when hydraulics are unknown

drought_fraction = exp(-L / max(7, reserve / 2))
```

The drought score is the mean across contributing anchors. Coverage is valid rain days divided by expected days. The fallback reserve and tolerance equation are screening policies.

### 7.4 Soil and elevation

| Component | Fractional score |
| --- | --- |
| pH | Ecological envelope evaluated at the supported pH |
| Texture | 1 for a preferred texture, 0.5 for another known texture, missing if unknown |
| Moisture context | `clamp(clamp((theta - WP)/(FC - WP),0,1) / (1-p),0,1)` |
| Elevation | 1 at/below a defined crop maximum, 0 above; missing if elevation or a crop maximum is absent |

Here `p` is the crop depletion fraction. Moisture context uses a supported full-depth root-zone value; it is distinct from date-matched initialization of the continuous water balance. Surface moisture and NDVI remain context and do not supply a substitute root-zone reservoir or verified crop history.

### 7.5 Establishment moisture used for date choice

For the first 21 days of each replayed crop window, use days with both rain and ET0:

```text
initial_supply = sum(rain × (1 - runoff_fraction))
initial_demand = sum(ET0 × initial_Kc)
establishment_fraction = clamp(initial_supply / max(1, initial_demand), 0, 1)
```

Average the resulting fractions across anchors with contributing observations. This date-choice diagnostic uses rain, not an inferred irrigation supply. Missing establishment evidence contributes zero to the search heuristic; that is a conservative search treatment, not a measured zero.

## 8. Simulate water continuously across the rotation

The full sequence, including intervening and trailing fallow, is simulated for every historical anchor. Storage is shared across the sequence and is not reset at each new crop.

### 8.1 Reservoir and initial uncertainty

Use a shared representative 0–1 m reservoir:

```text
capacity_mm = 1000 × (FC - WP)
```

When capacity is known, begin with two bounds: empty and full. An exact-date, 0–100 cm moisture observation at the first replay day can collapse those bounds:

```text
storage_mm = 1000 × clamp(theta - WP, 0, capacity_mm / 1000)
```

This observation is only checked at the start of a replay; it is not reapplied daily or at every crop transition. If rain or ET0 is missing on any day, skip that day's balance and reopen storage uncertainty to empty/full.

### 8.2 Crop coefficient through the cycle

Let `f` be elapsed days divided by `max(1, end_date - start_date)`:

```text
Kc(f) = initial_Kc                                      if f < 0.2
        linear initial_Kc → middle_Kc over f=0.2..0.5    if f < 0.5
        middle_Kc                                       if f < 0.8
        linear middle_Kc → end_Kc over f=0.8..1.0        otherwise
```

The 20/30/30/20 percent stage allocation is provisional. Fallow uses coefficient 0.2.

### 8.3 Daily supply, demand and extraction

For each known-weather day:

```text
effective_supply = rain × (1 - runoff_fraction) + explicit_irrigation_for_planned_date
demand = ET0 × Kc
```

An irrigation reliability answer does not create a volume. Only supplied dated amounts enter `explicit_irrigation_for_planned_date`.

For each low/high storage bound, when hydraulics are known:

```text
storage_after_supply = min(capacity_mm, storage + effective_supply)
root_fraction = min(1, crop.root_depth_m)  # fallow uses 0.25
accessible = storage_after_supply × root_fraction
root_capacity = capacity_mm × root_fraction
p = crop.depletion_fraction              # fallow uses 0.5
Ks = min(1, accessible / max(1e-9, (1-p) × root_capacity))
actual_ET = min(accessible, demand × Ks)
next_storage = max(0, storage_after_supply - actual_ET)
```

With unknown hydraulics, storage is not invented:

```text
actual_ET = min(effective_supply, demand)
next_storage = 0
```

For active crops, weight demand and actual ET by 1.5 over `0.5 <= f <= 0.8`, otherwise by 1. This adds mid-season sensitivity. Fallow affects storage but does not receive a productive-crop water component.

### 8.4 Water score and coverage

For each productive crop:

```text
water_fraction_low  = weighted_actual_ET_low  / weighted_demand
water_fraction_high = weighted_actual_ET_high / weighted_demand
water_coverage = known_rain_and_ET_days / expected_active_days
```

No positive accumulated demand means the water score is missing. The lower fraction is the ranking component; the upper fraction describes initial-storage sensitivity. Reported subtotals are only labeled complete when all expected days are known. The output field `effective_rain_subtotal_mm` accumulates effective supply and therefore includes explicit irrigation despite its field name.

## 9. Aggregate the rotation's environmental score

### 9.1 Component weights

| Component | Weight |
| --- | ---: |
| Water-demand satisfaction | 0.35 |
| Temperature | 0.20 |
| Drought exposure | 0.15 |
| Soil pH | 0.08 |
| Soil texture | 0.05 |
| Soil-moisture context | 0.07 |
| Terrain/cover exposure | 0.08 |
| Elevation | 0.02 |
| **Total** | **1.00** |

### 9.2 Terrain component

Across all selections:

```text
bare_fraction = 1 - 0.65 × productive_days / max(1, all_selection_days)
terrain_fraction = 1 - min(1, tan(slope) / tan(30°)) × bare_fraction
```

Convert slope angles to radians for `tan`. Unknown slope produces a missing terrain component. The coefficient 0.65 is assumed calendar cover; it is not predicted future NDVI, measured erosion or machinery-safety certification.

### 9.3 Combine crops

For each component other than terrain:

- Component score is the arithmetic mean of known productive-crop scores.
- Component coverage is the sum of those known crops' coverage divided by the number of crops providing that component slot.
- Missing crops do not contribute an observed score and reduce coverage.

Final crop-component means are **not weighted by crop duration**. The beam heuristic in Section 10 does use productive-day weighting; these are distinct calculations.

### 9.4 Supported bound, conditional score and interval

Let `w_i` be weight, `c_i` coverage, and `s_i` the 0–100 component score:

```text
known_weight = sum(w_i × c_i)                  over known components
lower_bound  = sum(w_i × c_i × s_i)            over known components
conditional_score = lower_bound / known_weight
```

If no weight is supported, conditional and ranking scores are null. Otherwise:

```text
ranking_score = lower_bound
coverage = known_weight
score = conditional_score
```

Missing evidence contributes no established points to the supported lower bound. It remains unknown rather than being reported as observed zero suitability.

The upper bound includes both unsupported components and water-storage uncertainty:

```text
water_uncertainty = mean(max(0, water_high_score - water_low_score))
                   over productive crops with a known water component
upper_bound = min(100,
    lower_bound + (1-known_weight) × 100
    + 0.35 × combined_water_coverage × water_uncertainty
)
score_range = [lower_bound, upper_bound]
```

Scores are rounded to three decimals and coverage to five. `confidence` is `limited` for coverage at least 0.5, otherwise `insufficient`. `calibrated` is always false.

For illustration, a conditional mean of 90 with coverage 0.5 gives a supported bound of 45. A mean of 75 with coverage 0.9 gives 67.5. The second ranks higher despite its lower conditional mean.

## 10. Generate candidates with bounded beam search

### 10.1 Initial state and eligible crops

Keep non-fallow crops whose zone IDs include the screening profile zone and whose systems include the declared system. Unknown/irrigated systems can also use conditional rainfed-reference candidates.

The search state is:

```text
(selections, next_available_date, productive_families,
 accumulated_date_score_times_days, productive_days)
```

Start with an empty sequence at the planning start. Search at most 20 expansion rounds; keep at most 20 selection entries, including fallow, and at most 32 states per beam.

### 10.2 Expand each partial sequence

For every eligible crop:

1. Skip it if its family equals the previous productive crop's family. Fallow does not reset this family history.
2. Skip a documented forbidden predecessor, looking back to the previous productive crop across fallow.
3. For the first selection, also check the supplied previous-crop history. The current code applies this history check when `selections` is empty.
4. Find the earliest supported sowing date at/after the state's cursor.
5. Try offsets of 0, 14, 28, 42 and 56 days from that date.
6. Keep only dates still inside an applicable sowing window.
7. Estimate thermal duration and discard an option ending after the planning horizon.
8. Score valid crop/date windows, sort by descending score then earlier sowing, and keep the best two per crop for this state.
9. Insert explicit fallow from the cursor through the day before sowing if there is a gap. If no fallow entry exists, that gap option cannot be appended.
10. Append the crop, advance to the day after its end, and update productive families and weighted score.

Sowing windows are inclusive and may cross calendar years. `_next_start` examines windows anchored from the cursor's previous year through two years ahead. If explicit window records exist but none qualify for the zone, month-only fallback does not unlock them. February 29 boundaries are clamped to the month's last day when necessary.

### 10.3 Window search score

Assess each crop as a standalone sequence using the same environmental engine:

```text
window_score = 0.85 × standalone_ranking_score + 15 × establishment_fraction
```

Missing standalone rank or establishment is zero in this search heuristic. The 15 percent establishment preference influences which dates/paths survive search. It is not an additional ninth weight in the final environmental score.

For each appended crop:

```text
accumulated_score += window_score × crop_duration_days
productive_days += crop_duration_days
```

### 10.4 Beam ordering and path diversity

Sort expanded states by:

1. Higher accumulated score divided by productive days.
2. More distinct productive families.
3. More productive crops.
4. Later cursor.
5. Lexicographic crop-ID/sowing-date sequence as a deterministic tie-break.

Reserve the first state for each distinct ordered productive crop path before adding alternative dates for existing paths. Truncate that combined list to 32 states. This prevents many date variants of one crop sequence from consuming the beam.

A nonempty state with no feasible expansion is placed in the finished set. After the loop, also retain the remaining beam. Empty states produce no candidates. The search favors continuing valid paths; it does not independently preserve every shorter prefix that could have stopped early.

## 11. Reassess and diversify completed candidates

1. Pad remaining time with trailing fallow when available and below the 20-entry limit.
2. Deduplicate exact crop/start/end sequences.
3. Reassess each full sequence with continuous water storage.
4. Sort by known rank before null rank, higher supported rank, higher coverage, then deterministic dated sequence.
5. Keep the best dated candidate for each ordered productive crop sequence. Ignore fallow when identifying that sequence.
6. Identify a crop combination by the sorted productive crop IDs, preserving repeated occurrences. Thus `wheat → lentil` and `lentil → wheat` share a combination; `wheat → lentil → wheat` is different.
7. Prefer distinct combinations before additional orderings of the same combination.
8. Take at most six, then sort those by environmental rank again.

Diversification is an explicit selection policy. The six returned paths need not be the six highest raw scores without that policy.

## 12. Shared evaluation and feasibility status

Every suggestion is passed to the same `evaluate` path used by custom rotations. It verifies plot/profile revisions, constructs crop instances, preserves explicit end dates and evaluates the full environment again.

Checks include supported calendars and farming systems; planning-horizon containment; date overlaps; documented predecessor restrictions; soil-pH guidance with observation qualification; machinery; labor capacity where quantities exist; operations and cost completeness; budget versus peak funding need; and historical climate coverage.

Generated family alternation is a conservative search policy. It is not a universal hard rule imposed identically on every manually supplied custom sequence. Fallow is not automatically accepted as a validated disease-breaking interval.

A component with score below 40 and coverage at least 0.8 adds an environmental-risk confirmation. It does not alone become a hard agronomic failure.

Status is:

```text
doesnt_fit          if any check fails
needs_confirmation  if no check fails and at least one is unknown
fits_current_inputs otherwise
```

Current live evaluation intentionally adds local-validation/assumption confirmations. A high environmental rank therefore commonly accompanies `needs_confirmation`; rank and feasibility status answer different questions.

### 12.1 Historical exposure diagnostics

In addition to scoring, the evaluator reports common-anchor historical crop windows with rain subtotals, complete totals where supported, coverage, confirmed dry spells, diagnostic Tmax above 35°C and Tmin below 0°C, and rain onset.

Rain onset is the first three-day sum reaching the scenario threshold, default 20 mm, within the first 60 crop-window days. Earlier missing rain can make the onset uncertain. This diagnostic is not a separate rule that automatically determines sowing or converts weather into yield.

### 12.2 Economic enrichment

New live evaluations resolve historical producer prices and national yields from FAOSTAT, reviewed regional ONCA seasonal costs, and applicable imported local records. Producer/farm-gate observations can take precedence; retail values are not automatic farm-sale prices. The normal farmer client clears financial overrides before requesting suggestions.

Revenue uses reference yield × reference price × hectares. Seasonal costs reserve the selected per-hectare budget without charging the same costs again as detailed operations. Operations, labor, cash timing and peak funding are calculated where supported. See [economics](economics.md) for exact reference selection and scope.

Environmental stress does not apply a calibrated yield/revenue reduction. Economics is resolved for evaluated candidates after the environmental generator has shortlisted them.

## 13. Backend ranking versus displayed cards

### 13.1 API ordering

For live candidates, `Service.suggest` sorts lexicographically by:

```text
(
    has_agronomic_failure,
    ranking_score_is_null,
    negative_ranking_score,
    negative_environmental_coverage,
    secondary_value_is_missing,
    secondary_value
)
```

False sorts before true. For `has_agronomic_failure`, the backend excludes failures with codes `BUDGET_CHECK`, `LABOR_CAPACITY_CHECK`, `MONTHLY_LABOR_CHECK` and `MACHINERY_ACCESS_CHECK`. Those can still make the overall status `doesnt_fit`.

Only the first priority is used. Margin/profit priorities prefer higher operating margin; `low_labor` prefers fewer paid plus family person-days. Other/default priorities use lower peak funding need as a secondary value. Missing secondary values sort after known values.

Therefore economics/labor only break ties after the backend's agronomic-failure group, supported environmental score and coverage. They never drive beam survival.

### 13.2 Current frontend ordering

`App.tsx` performs an additional presentation pass:

1. Deduplicate ordered productive crop IDs, excluding fallow. If duplicates arrive, its map retains the last value for that path.
2. Sort by descending `environmental.ranking_score` for live plots; null scores are treated as negative infinity.
3. Preserve incoming order when environmental scores tie in the current environment sort mode.
4. Display the first three cards.

The UI does not explicitly repeat the backend's agronomic-failure grouping before its environmental re-sort. A higher-scoring card can therefore move ahead of a lower-scoring card that had a better backend feasibility group. Status badges remain attached. This document records that behavior; it does not change the ordering code.

### 13.3 Duration changes and stale requests

Changing the duration clears old cards/current evaluation, marks inputs dirty, increments the work token and queues automatic suggestions. The effect waits until a plot, analysis and catalog exist, analysis is no longer running, no other operation is busy, and the boundary is confirmed.

If an older suggestion request completes after inputs changed, token checks prevent it from replacing current results. A queued request runs with the latest duration once the prior operation finishes. No extra Compare click is needed. Saved evaluation snapshots are not rewritten.

## 14. End-to-end pseudocode

```text
SUGGEST(request):
    plot = load_project_plot(request.plot_id)
    require_current_revision(plot, request.plot_revision)
    catalog = load_catalog_for_mode(plot)

    if plot.mode == demo:
        candidate_requests = generate_labeled_fixture_templates(request)
    else:
        profile = get_current_analysis(plot)
        profile = prepare_qualified_screening_copy(plot, profile, request.constraints)
        env = EnvironmentalModel(profile, request.constraints, request.start_date)
        beam = [empty_state_at(request.start_date)]
        finished = []

        repeat at most 20 times:
            expanded = []
            for state in beam:
                options = []
                for crop in eligible_productive_crops(catalog, profile, constraints):
                    if violates_generated_family_or_predecessor_policy(state, crop):
                        continue
                    earliest = earliest_applicable_sowing_date(state.cursor, crop)
                    dates = earliest + each of [0,14,28,42,56] days
                    valid = []
                    for start in dates:
                        require_applicable_window_or_skip(start, crop)
                        duration = env.estimate_duration(crop, start)
                        end = start + duration - 1
                        if end <= planning_end:
                            valid.append(score_window(crop, start, end))
                    for option in best_two(valid):
                        options.append(append_with_gap_fallow(state, option))
                if options:
                    expanded.extend(options)
                else if state is nonempty:
                    finished.append(state)
            if expanded is empty:
                break
            beam = best_32_with_productive_path_diversity(expanded)

        finished.extend(beam)
        rotations = deduplicate_and_pad_trailing_fallow(finished)
        ranked = sort_by_full_continuous_environmental_assessment(rotations)
        unique_paths = best_dates_for_each_productive_path(ranked)
        candidate_requests = prefer_distinct_combinations_then_take_six(unique_paths)

    evaluations = [shared_evaluate(candidate) for candidate in candidate_requests]
    return apply_backend_feasibility_environment_secondary_order(evaluations)

DISPLAY(evaluations):
    distinct = deduplicate_productive_paths(evaluations)
    return first_three(sort_by_environmental_ranking_score(distinct))
```

This pseudocode summarizes the stages; the source functions define exact errors, tie-breaks, fallback behavior and evidence fields.

## 15. Custom rotations, demo mode and persistence

Custom rotations call the shared evaluator directly and bypass generated-candidate pruning/diversification. Explicit start/end dates are preserved, while overlaps, horizon violations and applicable rules are checked. The API permits up to 20 selection entries.

Demo mode uses explicitly synthetic templates: alternating `wheat_demo → faba_demo` or `barley_demo → faba_demo`, extending at most ten productive entries while they fit the horizon. It uses fixture sowing months and nominal durations, not live environmental beam search. Synthetic results remain labeled.

Saved evaluations freeze their request, area, profile and catalog. The evaluator creates a SHA-256 fingerprint from the sorted serialized input snapshot. New observations, catalogs or duration changes create new evaluations rather than silently changing existing saved results.

## 16. Limits and interpretation

- The 32-state beam, five date offsets, two dates retained per crop, 20-entry limit and six-candidate limit make this a bounded heuristic. It does not prove the global optimum over all possible crops, dates or management systems.
- It does not guarantee three suggestions. No crop fitting the period, unsupported calendars or insufficient candidate diversity can reduce the result count.
- Family alternation, thermal targets, generic Kc/stage allocation, soil hydraulic mapping, runoff, drought tolerance and score weights are explicit policies/approximations needing Moroccan field/cultivar validation.
- Rooting access is capped at 1 m in the modeled reservoir even when a crop parameter lists a deeper root system.
- Missing weather remains missing; missing storage/soil inputs broaden uncertainty or invoke the explicitly labeled no-storage fallback.
- The model does not calibrate vernalization, photoperiod, nitrogen transfer, pests, disease populations or a weather-to-yield response.
- Historical replay is not a weather forecast. A score is not a yield prediction or probability of crop success. National historical economics is not a current field-specific quote.
- Precise UI ordering, API ranking, feasibility status and environmental rank are separate parts of the implementation and should be assessed separately.

## Appendix A. Current crop parameters

These tables reproduce the reviewed YAML values. The linked fixture remains authoritative if parameters change.

### A.1 Thermal, water and root parameters

| Crop | Cycle min–max | Base / cap °C | GDD target [low–high] | Kc initial / middle / end | Root m | Depletion p |
| --- | --- | --- | --- | --- | ---: | ---: |
| `wheat` | 90–250 | 0 / 26 | 2100 [1700–2700] | 0.7 / 1.15 / 0.3 | 1.5 | 0.55 |
| `durum_wheat` | 120–180 | 0 / 26 | 2100 [1700–2500] | 0.7 / 1.15 / 0.3 | 1.0 | 0.55 |
| `barley` | 90–240 | 0 / 26 | 1700 [1400–2100] | 0.3 / 1.15 / 0.25 | 1.0 | 0.55 |
| `faba` | 100–235 | 0 / 30 | 2100 [1700–2600] | 0.5 / 1.15 / 0.3 | 0.6 | 0.45 |
| `lentil` | 70–240 | 0 / 30 | 1900 [1500–2400] | 0.4 / 1.1 / 0.3 | 0.7 | 0.5 |
| `chickpea` | 90–180 | 0 / 30 | 2000 [1600–2500] | 0.4 / 1.15 / 0.35 | 0.8 | 0.5 |

Thermal targets are degree-days; cycle limits are days; roots are meters. Kc entries are initial/middle/end. The thermal calculation does not apply the ecological temperature-envelope limits as its base/cap.

### A.2 Ecological envelopes

| Crop | Temperature °C | pH | Preferred textures | Maximum elevation m |
| --- | --- | --- | --- | ---: |
| `wheat` | 5 / 15 / 23 / 27 | 5.5 / 6 / 7 / 8.5 | medium | 4500 |
| `durum_wheat` | 6 / 17 / 25 / 30 | 5.5 / 6 / 7 / 8.5 | medium, heavy | — |
| `barley` | 2 / 15 / 20 / 40 | 6 / 6.5 / 7.5 / 8 | medium | 4400 |
| `faba` | 5 / 18 / 28 / 32 | 4.5 / 6 / 7 / 8.6 | medium | 2700 |
| `lentil` | 5 / 15 / 29 / 32 | 4.5 / 5.5 / 7.5 / 8.2 | medium, heavy | 3800 |
| `chickpea` | 7 / 15 / 29 / 35 | 4.7 / 6 / 8.5 / 9.5 | medium, heavy | 2300 |

Each four-number envelope is low / optimum-low / optimum-high / high. A dash means no maximum elevation is specified and that component is unavailable for that crop. These species-scale references do not establish local cultivar calibration.

## Appendix B. Related documentation and regression coverage

- [Product scope](product-spec.md)
- [Model/source inventory](model-data-sources.md)
- [Agronomic references](agronomy.md)
- [Economic references](economics.md)
- [Verification instructions](testing.md)
- [Environmental regression tests](../apps/api/tests/test_environmental_selection.py): thermal missingness/censoring, date sensitivity, ET equations, water continuity/fallow recharge, leap days, field overrides, replay, environmental independence from economic overrides and distinct productive sequences.
- [Agronomy tests](../apps/api/tests/test_agronomy.py): qualified windows, crop rules and exposure behavior.
- [Economic tests](../apps/api/tests/test_economic_references.py): source precedence, stable snapshots and prevention of cost double counting.
