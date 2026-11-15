# Earth data integration

The current automatic acquisition path is documented in [data setup](data-setup.md). `AGRIX_REMOTE_ASSETS=1` enables bounded SMAP, terrain, soil and HLS acquisition during plot analysis. The contracts below also cover identified local imports. See [model sources](model-data-sources.md) for current decision roles.

`agrix.earth.ProfilePipeline` builds real profiles from public NASA POWER and explicitly registered local scientific subsets. Its version is `earth-profile-1.0.0`. A failed or absent source produces a missing value and a reason. Real profiles and all their evidence have `synthetic=false`.

## Running it

The application reads `AGRIX_CACHE_DIRECTORY` (default `data/cache`) and `AGRIX_PROVIDER_NETWORK` (default `true`). `AGRIX_DEFAULT_ANALYSIS_MODE=live` selects real processing; demo profiles remain a separate application mode. A first live analysis needs internet access for POWER unless matching UTC data are already registered or cached. Earthdata credentials are unnecessary for POWER.

```python
from datetime import date
from pathlib import Path
from agrix.earth import ProfilePipeline

pipeline = ProfilePipeline(Path("data/cache"), network_enabled=True)
profile = pipeline.run(existing_plot, date(2024, 10, 1), date(2024, 10, 31))
```

`existing_plot` is a validated `agrix.models.Plot`, including its real polygon. Importing this module does not make network requests. `progress_callback=callback` receives `LayerCoverage` updates with `running`, followed by `complete`, `partial`, `failed`, or `unsupported`. To retry an independent layer while retaining completed weather:

```python
updated = pipeline.run(existing_plot, start, end,
                       previous_profile=profile, retry_layers=["terrain"])
```

The earlier profile must belong to the same plot revision. Use the same period for retries. The application job layer also validates and caches analysis requests. `pipeline.health()` reports configuration, dependency presence, registered datasets, declared units and whether Earthdata environment credentials are configured. A configured provider is not a claim that its remote endpoint is currently reachable.

Rain-only and temperature-only retries replace only their selected daily variables and evidence IDs. They retain the other variable's daily values, metric, layer and source lineage, including when its input file is no longer available. The resulting combined daily series is cached as its own Parquet asset. A targeted retry with a different existing weather period is rejected; start a new complete analysis for a new period.

## Provider behavior

| Layer | Implemented input | Result and limitations |
|---|---|---|
| Temperature | Public POWER daily API; imported UTC POWER JSON; declared local daily Parquet | Daily minimum, maximum and mean, plus period mean temperature. Fill values remain null. Regional meteorological cells are approximately 0.5° × 0.625°, not plot weather stations. |
| Rainfall | Registered IMERG Final, Late or Early Giovanni CSV; dated raster subsets; POWER fallback | Prefer a complete homogeneous product, then Final, Late, POWER, Early. A partial Final run does not silently replace complete fallback data. Total includes known days and is marked partial if dates are missing. Runs are never merged under a Final label. |
| Soil properties | Local GeoTIFF or regular CF latitude/longitude NetCDF; optional SoilGrids WCS | Preserves depth, scaling and supplied uncertainty. Each supplied soil depth has a separate metric; the shallowest interval is the primary context. No inferred depth average. |
| Soil moisture | Local SMAP or other explicitly identified subsets | Volumetric moisture in m³/m³. The primary `soil_moisture` requires declared 0–100 cm depth. A 0–5 cm layer is separately named `soil_moisture_surface`. |
| Terrain | Local georeferenced DEM with explicit elevation unit; precomputed slope GeoTIFF | DEM slope uses native gradients and metric horizontal distances. A one-pixel DEM without neighboring elevations cannot yield slope. |
| Vegetation | Local aligned HLS red, NIR and Fmask GeoTIFFs | NDVI after scale and cloud, shadow, snow, adjacency and water masking. NDVI does not establish crop identity or verified crop history. |

POWER requests explicitly use UTC, source units and fill values. Nearby plots reuse requests at native grid centers. Polygons intersecting multiple cells use geodesic area weights and require valid values in every contributing cell for each daily average. Large requests spanning more than 16 weather cells are rejected. [POWER daily API documentation](https://power.larc.nasa.gov/docs/services/api/temporal/daily/).

Registered raster pixels are weighted by their actual intersection with the polygon. Nodata, quality flags and uncovered area remain explicit. Processing preserves native resolution; it never creates field precision by resampling coarse cells. Regular CF NetCDF grids use xarray scale/fill decoding, temporal clipping and bounded spatial windows. Projected SMAP EASE-Grid HDF5 requires a georeferenced provider subset, rather than assigning longitude and latitude to its x/y coordinates. Root-zone and surface depth definitions come from the [SMAP L4 specification](https://nsidc.org/sites/default/files/documents/technical-reference/smap_l4_sm_psd_v17_for_l4sm_version_8.pdf). HLS masking follows the [NASA HLS product documentation](https://hls.gsfc.nasa.gov/data-products/).

SoilGrids WCS is enabled explicitly with `AGRIX_SOILGRIDS_WCS=1`. It fetches a bounded native 250 m pH 0–5 cm mean subset; other properties, depths and quantiles can be registered locally. REST beta is documented as paused; WCS is the optional backend. ISRIC explicitly advises against treating SoilGrids as validated farm-level measurements. Evidence retains this caveat and absent uncertainty remains unknown. Plot soil tests should override only their supported property and depth in the application record layer. [ISRIC access documentation](https://docs.isric.org/globaldata/soilgrids/SoilGrids_faqs_02.html), [WCS examples](https://docs.isric.org/globaldata/soilgrids/WCS_from_Python.html), [scale and uncertainty guidance](https://docs.isric.org/globaldata/soilgrids/SoilGrids_faqs_04.html).

## Registering local files

Registering copies a selected file into an immutable SHA-256 path inside the managed cache and updates `assets.json`. Paths outside the cache are rejected when reading a manifest. A Python download script is executable code, not a scientific data asset: download the corresponding selected data files first.

The command-line entrypoints use the same validated registration functions:

```sh
.venv/bin/agrix provider-health
.venv/bin/agrix import-asset /path/to/utc-power.json --dataset-id NASA_POWER_DAILY --format power_json
.venv/bin/agrix import-asset /path/to/giovanni-final.csv --dataset-id GPM_3IMERGDF --format giovanni_csv --version 07
.venv/bin/agrix import-records /path/to/sourced-market-or-services.csv --project-id demo
.venv/bin/agrix prepare-pilot --project-id demo
```

`import-asset` supports `--variable`, `--unit`, `--version` and `--metadata-json FILE`. The metadata file is a JSON object containing depth, period, bounds, scaling, quality-mask paths and source information. Raster imports require variable and unit. POWER/CSV adapters validate UTC and exact product identity before registration. `provider-health` performs local configuration and integrity checks; it makes no remote requests. Local-record import accepts CSV, a JSON list or `{"records":[...]}`, validates the whole batch and commits it transactionally to the configured database. It preserves source prices/units and is idempotent per project/kind/source ID.

`prepare-pilot` is an explicit, idempotent setup action. It creates/reuses an illustrative live Settat polygon with unknown farm inputs, commits the plot and analysis job before starting processing, and waits up to 60 seconds by default (`--wait-seconds`, maximum 120). It analyzes the two farming seasons 2023-11-01 through 2025-10-31. A completed/partial profile can produce a saved **draft** wheat plan needing confirmation. Any economic references remain labeled estimates, and unknown farm observations remain unknown. Its boundary is not a verified parcel and its dates are illustrative. Failed or still-running analysis yields a poll/retry action and no fabricated evaluation. Repeated setup preserves user edits and reuses the matching analysis/draft; it is not an automatic startup seed.

```python
from pathlib import Path
from agrix.earth import register_asset

cache = Path("data/cache")
register_asset(cache, Path("/path/to/utc-power.json"),
               "NASA_POWER_DAILY", "power_json", version="actual-response-version")
register_asset(cache, Path("/path/to/giovanni-final.csv"),
               "GPM_3IMERGDF", "giovanni_csv", version="07")
register_asset(cache, Path("/path/to/phh2o_0-5cm_mean.tif"),
               "SoilGrids", "geotiff", variable="soil_ph", unit="pH*10",
               version="2.0", depth_cm=[0, 5],
               source_url="https://soilgrids.org/",
               uncertainty="No prediction interval supplied")
register_asset(cache, Path("/path/to/dem.tif"),
               "SRTMGL1", "geotiff", variable="elevation", unit="m",
               version="downloaded-product-version")
```

The IMERG CSV must identify its run and aggregation bounding box; the actual file identity is checked against the registered run. Its export box must cover the plot. A regional area-average CSV cannot be turned into individual plot pixels. Native retrieval counts and uncertainty absent from such an export are called out in evidence. POWER JSON must explicitly identify UTC; an LST file cannot be relabeled as UTC without re-exporting the source.

For HLS, register `variable="ndvi", unit="index"` with `path` set to the red file and also pass `red_path`, `nir_path`, `quality_path`, and `reflectance_scale=0.0001`. L30 uses red B04 / NIR B05; S30 uses red B04 / narrow NIR B8A. Keep all three files on the same native grid and supply the acquisition period. Companion files are copied, hashed and returned as profile assets.

For other quality masks, supply `quality_path` and either `quality_valid_values=[...]` or `quality_invalid_bits=<bitmask>` based on the actual product documentation. The mask must match the scientific raster grid. A nodata mask alone does not imply all native product quality tests passed.

The manifest schema is an object with `version` and `assets`, for example:

```json
{
  "version": "1.0",
  "assets": [
    {
      "id": "unique-asset-id",
      "dataset_id": "SPL4SMGP",
      "version": "008",
      "format": "geotiff",
      "path": "assets/local-subset.tif",
      "sha256": "actual-file-sha256",
      "variable": "soil_moisture",
      "unit": "m3/m3",
      "depth_cm": [0, 100],
      "start": "2024-10-01",
      "end": "2024-10-01",
      "source_url": "https://nsidc.org/data/spl4smgp"
    }
  ]
}
```

Supported formats are `power_json`, `giovanni_csv`, `geotiff`, `netcdf`, `parquet`. Raster metadata also accepts `band`, `scale`, `offset`, `canonical_unit`, `original_unit`, `uncertainty`, native quality rules, and `bounds`. When applying explicit `scale`/`offset`, declare `unit`/`canonical_unit` as the resulting scientific unit and retain the stored unit separately in `original_unit`; do not request the same conversion twice. A precomputed daily rainfall raster must declare one day through `start` and `end`, and units `mm`, `mm/day`, `m`, or `m/day`; it is never broadcast across a month. A NetCDF entry additionally accepts `data_variable`, `latitude_name` and `longitude_name`. Local weather Parquet requires `variable="weather"`, daily canonical columns and its original aggregation `bounds`.

## NASA protected datasets

Create the free [Earthdata Login account](https://urs.earthdata.nasa.gov/users/new) outside the application. Credentials belong in the server environment as `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD`; never put them in the manifest, browser or source control. `earthaccess` authentication uses environment strategy with `persist=False`. [Official authentication guide](https://earthaccess.readthedocs.io/en/stable/user/howto/authenticate/).

`agrix.earth.providers.discover_nasa_granules(collection, version, bounds, start, end, limit=10)` searches CMR with explicit region, date, version and a result limit. A search bounding box filters matching granules; it does not spatially crop their contents. Prefer provider Harmony/Earthdata Search spatial and variable subsets for IMERG, SMAP and HLS. Register the resulting GeoTIFF/NetCDF subsets. The profile pipeline never automatically downloads global NASA granules. A separate `download_selected_granules` helper accepts at most ten explicitly selected granules and requires `allow_full_granules=True` to acknowledge full-file transfer. [Official download workflow](https://earthaccess.readthedocs.io/en/latest/user/howto/download-to-local-machine/).

## Provenance, cache and validation

Raw POWER JSON and optional WCS GeoTIFFs are cached under `raw/`. Processed weather is a compressed Parquet file with date, rain, minimum/mean/maximum temperature and evidence IDs. Its identity includes processing version, source evidence, polygon and requested period; a JSON sidecar retains that identity. Profile `assets` return raw and processed paths, exact SHA-256 digests, periods and variables. Missing dependencies produce explicit provider failure; without pyarrow, the standalone adapter can write a truthfully labeled JSON fallback.

Every metric, daily value and layer resolves to profile evidence. Evidence records source version, requested data period, geographic support, native resolution, units, depth, quality limits and uncertainty. No quality interval or field validation is invented. The Earth pipeline initially marks agronomic rule coverage unsupported; the regional rule engine resolves coverage separately.

Run the deterministic verification suite with:

```sh
.venv/bin/pytest -q apps/api/tests/test_earth_pipeline.py apps/api/tests/test_earth_cli.py
```

These tests use mocked HTTP, small GeoTIFF/CF NetCDF files, native-area nodata handling, DEM gradients, cloud masks, unit conversion, lineage and retries. The recorded [NASA acquisition evidence](evidence/general-nasa-verification.json) describes bounded five-year checks at several Moroccan locations. Provider access checks do not establish field measurement accuracy or complete coverage for every plot.
