# General NASA dataset setup

Updated 2026-11-15. The original NASA sources are retained. Defaults cover the **five latest complete years: 2021-01-01 through 2025-12-31** in this release; environment defaults roll forward with the calendar.

## Working retrieval

| Input | Product/access route | Behavior |
|---|---|---|
| Rain, temperature, humidity, wind, radiation | Existing NASA POWER daily API | Requests native cells intersecting any supported polygon; caches daily responses |
| Root-zone and surface water | NASA SMAP L4 distributed in NASA POWER's public Zarr archive | Reads only intersecting cells and requested dates; root zone 0–100 cm and surface 0–5 cm remain separate |
| Terrain | NASADEM HGT v001, NASA SRTM reprocessing, through the public Planetary Computer mirror | HTTP range reads of native COG windows, including slope neighbors; no whole global DEM |
| Vegetation | Original NASA HLS L30/S30 v2 through NASA's documented Azure mirror | Red/NIR/Fmask native windows; masked NDVI; configurable distributed temporal sampling |
| Soil | Existing ISRIC SoilGrids WCS | pH, sand, silt, clay subsets; empty/zero padding remains missing |
| IMERG Final | Existing identified file/subset adapter and authenticated Earthdata access | Still requires a selected source download/import; it is not silently replaced or claimed downloaded |

NASA mirrors preserve the scientific products. **ERA5, Open-Meteo and Copernicus DEM have not replaced any established source.**

## Local credentials

Protected products require local Earthdata credentials. Configure them locally:

```bash
.venv/bin/agrix earthdata-login
```

This prompts locally through `earthaccess` and persists its standard local credentials. Protected NASA downloads accept configured environment credentials or the local `.netrc`. Never commit credentials or paste passwords into chat.

## Automatic requests for new plots

Configure the server:

```bash
export AGRIX_PROVIDER_NETWORK=true
export AGRIX_REMOTE_ASSETS=1
export AGRIX_HLS_MAX_SCENES=10
export AGRIX_ANALYSIS_START=2021-01-01
export AGRIX_ANALYSIS_END=2025-12-31
```

Keep your existing `DATABASE_URL`; do not initialize a replacement database. Starting a plot analysis now requests bounded SMAP, soil, terrain and HLS subsets as well as the existing NASA weather. Provider failures leave independent layers available and explicit gaps. Existing saved plans remain frozen.

Inspect configured access without printing secrets:

```bash
.venv/bin/agrix provider-health
```

## Pre-download for any location

Plan the source requests:

```bash
.venv/bin/agrix data-plan --bbox -5.56 33.89 -5.54 33.91
```

Download and register bounded scientific subsets:

```bash
.venv/bin/agrix fetch-data --geojson /path/to/plot.geojson
.venv/bin/agrix fetch-data --bbox -5.56 33.89 -5.54 33.91 \
  --start 2021-01-01 --end 2025-12-31 \
  --datasets smap,soil,terrain,hls --max-scenes 60
```

The GeoJSON can be a Polygon or a Feature containing a Polygon. Weather is requested by the normal analysis pipeline. Use the same cache directory for CLI and application (`AGRIX_CACHE_DIRECTORY`, default `data/cache`). Raw inputs, decoded subsets, hashes and acquisition reports are retained there.

HLS `--max-scenes 60` requests a representative scene in each sampled month over five years. It is not every satellite overpass. Default 10 scenes is bounded historical context and must not be described as complete imagery coverage. Cloud masks may leave an image with no usable plot pixels.

The implementation bounds SMAP to 64 intersecting native cells and individual raster windows to two million pixels. Regional bulk preparation should partition a large region into tiles/plot groups. Do not request all Morocco at 30 m in one processing call.

## If you want full offline coverage

Full global archives are unnecessary. For a chosen region:

1. Cache five-year POWER source cells through normal plot analysis.
2. Use `fetch-data` on bounded regional polygons for SMAP. It transfers required Zarr chunks rather than the global store.
3. Download one static SoilGrids/DEM set; these are not five annual products.
4. Set the desired HLS sample density, or obtain selected native red/NIR/Fmask files through Earthdata and import identified subsets.
5. For IMERG Final, use Earthdata Search with the original **GPM_3IMERGDF collection 07**, chosen bounding box and 2021–2025 dates. Request spatial/variable subsets where offered. Retain actual run/version and quality fields. Import supported outputs using `agrix import-asset`; the existing Giovanni CSV path also remains supported. A CMR bounding-box search filters granules but does not crop them.

Native global SMAP/HLS downloads can be huge. The bounded request path is preferable. Existing selected-full-granule helpers require an explicit full-file opt-in and never run automatically during plot analysis.

## Meaning of water availability

The model now has a general NASA source for **daily water already in the root zone**, alongside precipitation and calculated evaporative demand. Dated SMAP can initialize the historical soil-water balance; future dates never inherit an unrelated old observation.

This does not prove a parcel can pump a well or receive reservoir/canal water. Those require actual supply measurements, an allocation/delivery record and farmer confirmation. Optional explicit irrigation assumptions can be submitted as:

```json
{
  "irrigation_mm_by_date": {"2026-11-15": "20"},
  "irrigation_source_label": "Farmer delivery schedule; still to confirm",
  "irrigation_verified_on": null
}
```

Only supplied dated amounts enter the water balance; marking supply reliable does not invent a volume. Water assumptions and their verification remain separate from satellite observations.

## Actual verification and limitations

Live retrieval was exercised at **Settat, Meknes and Agadir**, not only the original sample plot. Each five-year weather profile contains 1,826 days. SMAP returned 1,825 valid root-zone days in the tested requests; the missing day is preserved. Terrain was retrieved for all three. HLS samples were retrieved successfully for several years at the first test footprint. Some SoilGrids requests return empty padding; those must remain missing rather than zero pH or perfect soil coverage.

Broad environmental screening is available beyond the small pilot lookup boxes, with explicit partial rule coverage. It is not a nearest-region assignment or official local calendar approval. Region-specific chickpea calendars remain restricted to their documented regions. Unknown production systems still require confirmation.

The crop/rotation engine works with these data, but its provisional thermal targets, soil hydraulics, runoff and ranking policy still require local agronomic calibration. More downloaded data cannot manufacture that validation.

Sources: [NASA POWER daily API](https://power.larc.nasa.gov/docs/services/api/temporal/daily/), [NASA POWER SMAP hydrology](https://power.larc.nasa.gov/docs/methodology/data/sources_hydrology/), [NASA HLS access/mirrors](https://hls.gsfc.nasa.gov/data-access-and-tools/), [NASADEM mirror](https://planetarycomputer.microsoft.com/dataset/nasadem), [ISRIC WCS](https://docs.isric.org/globaldata/soilgrids/WCS_from_Python.html).
