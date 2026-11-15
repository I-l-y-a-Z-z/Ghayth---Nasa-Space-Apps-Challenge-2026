# Model and data sources

The engine ranks rotations by environmental suitability and supported evidence. Historical economics is secondary. Runtime parameters and source identities belong in versioned fixtures and the registry rather than duplicated documentation tables.

The [full rotation algorithm](rotation-algorithm.md) documents the implemented search, thermal timing, water balance, scoring equations, uncertainty, feasibility checks and final selection.

## Inputs

| Input | Source | Canonical quantities and use |
| --- | --- | --- |
| Field | Confirmed GeoJSON Polygon | WGS84 longitude/latitude; geodesic hectares |
| Weather | NASA POWER; identified IMERG rainfall imports | Rain mm/day; mean/min/max temperature °C; optional humidity, wind and solar radiation |
| Moisture | NASA SMAP L4 via POWER daily Zarr or identified subsets | Volumetric m³/m³; root zone 0–100 cm and surface 0–5 cm remain distinct |
| Soil | ISRIC SoilGrids and supported field tests | pH, texture fractions and declared depth, with uncertainty |
| Terrain | NASADEM/SRTM subsets | Elevation m; slope from native neighboring pixels |
| Vegetation | HLS L30/S30 red, NIR and Fmask | Quality-masked NDVI; ground-cover context, not crop history |
| Crop rules | Qualified Moroccan cereal/legume guides | Sowing windows, constraints and operation templates |
| Environmental parameters | FAO56, species ecology and screening assumptions | Temperature/pH envelopes, Kc, rooting depth and thermal targets |
| Farm facts | Farmer or documented field records | Irrigation, dated supply, equipment, crop history and measurements |
| Economics | FAOSTAT prices/yields, ONCA budgets, imported records | MAD/kg, kg/ha, MAD/ha; [calculation](economics.md) |

[data/source_registry.yaml](../data/source_registry.yaml) records coverage, access, units and inspected references. [Data setup](data-setup.md) explains acquisition; [Earth integration](earth-integration.md) documents extraction/cache behavior. Defaults cover the five latest complete calendar years. Static terrain/soil need one acquisition. Bounded HLS sampling does not establish complete imagery coverage.

## Environmental calculations

- Search dates within supported calendars and estimate maturity from historical growing-degree accumulation. Preserve explicit end dates and expose fallbacks, unreached maturity and thermal-target sensitivity.
- Use FAO56 Penman–Monteith ET0 with complete required meteorology; otherwise use labeled temperature-based Hargreaves. Missing temperature does not receive invented ET0.
- Carry daily water storage across crops and fallow using rain, crop demand and explicit irrigation. Texture-based hydraulic references retain uncertainty. Dated root-zone moisture can initialize the corresponding historical storage.
- Assess thermal exposure, pH, texture, water deficit, dry spells, terrain and cover where supported. Missing days widen uncertainty.
- Rank by the conservative supported environmental bound and coverage. Economics can break environmental ties; it does not determine which candidates survive environmental search.

Versioned definitions:

- [Crop catalog](../apps/api/src/agrix/fixtures/morocco_catalog.yaml)
- [Environmental parameters/policy](../apps/api/src/agrix/fixtures/environmental_rules.yaml)
- [Environmental engine](../apps/api/src/agrix/agronomy/environment.py)
- [Rotation search](../apps/api/src/agrix/agronomy/planning.py)
- [Economic source snapshot](../apps/api/src/agrix/fixtures/morocco_economics.json)

## Evidence and limits

Evidence preserves product/run/version, dates, units, resolution, depth, conversions, quality notes and source hashes where applicable. Missing data, unsupported rules and provider failures are distinct states. POWER rainfall is not relabeled IMERG Final; Early/Late/Final imports keep their identities.

SMAP/rainfall describe crop-water context rather than access to wells, canal deliveries or reservoir allocations. Marking irrigation reliable creates no quantity. NDVI does not identify previous crops. SoilGrids does not replace a field test.

Thermal targets, stage fractions, hydraulic approximations, runoff and scoring weights require local validation. Vernalization and photoperiod responses are not calibrated. Scores are comparative indices; national historical yields are budgeting references. No calibrated weather-to-yield or weather-to-revenue model is claimed.

Next validation inputs include Moroccan cultivar phenology, measured soil/moisture, field weather comparisons, irrigation deliveries, independent harvest records and current producer/contractor quotes. Recorded smoke checks are under [evidence/](evidence/); see [testing](testing.md) for their scope.
