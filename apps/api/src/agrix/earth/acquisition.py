"""Bounded on-demand acquisition of the existing NASA/SoilGrids products.

Mirrors change the access route, never the scientific product. No global granule
download is performed. Static soils/terrain are not downloaded once per year.
"""

import hashlib
import json
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import httpx
from shapely.geometry import box, shape

from agrix.earth.manifest import atomic_json, register_asset
from agrix.earth.providers import SoilGridsWCS
from agrix.earth.records import ProviderError

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SMAP_STORE = "s3://nasa-power-smap/smap/temporal/power_smap_daily_temporal_utc.zarr"


def five_year_period(today=None):
    today = today or date.today()
    return date(today.year - 5, 1, 1), date(today.year - 1, 12, 31)


def acquisition_plan(bounds, start, end):
    return {
        "bounds": list(bounds),
        "start": str(start),
        "end": str(end),
        "sources_preserved": True,
        "datasets": [
            {
                "id": "NASA_POWER_DAILY",
                "access": "public daily point API; native-cell area weights",
                "credentials": False,
            },
            {
                "id": "NASA_POWER_SMAP_L4",
                "access": SMAP_STORE,
                "credentials": False,
                "note": "NASA-distributed daily SMAP L4 via POWER, not MERRA-2 wetness.",
            },
            {
                "id": "NASADEM_HGT",
                "access": "NASA SRTM-reprocessed DEM COGs; Planetary Computer mirror",
                "credentials": False,
                "temporal": "static",
            },
            {
                "id": "HLS_v2",
                "access": "NASA HLS L30/S30 COGs; NASA-endorsed Azure mirror",
                "credentials": False,
            },
            {
                "id": "SoilGrids",
                "access": "ISRIC WCS native subsets",
                "credentials": False,
                "temporal": "static",
            },
            {
                "id": "GPM_3IMERGDF",
                "access": "Earthdata Harmony selected spatial/temporal subsets",
                "credentials": True,
                "note": "Preserve Final identity. Until supplied, retain explicitly labeled NASA POWER rain fallback.",
            },
        ],
        "water_supply": "NASA rainfall, SMAP root-zone moisture and ET demand estimate crop water availability. They do not prove access to a well, reservoir allocation or irrigation rights.",
        "bulk_guidance": "Use the same bounds and period for bounded regional downloads. Full global SMAP/HLS granules are unnecessary. Soils and DEM need one static acquisition, not five annual copies.",
    }


class DataAcquirer:
    def __init__(self, cache_dir, *, client=None, network_enabled=True):
        self.cache = Path(cache_dir).resolve()
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.network_enabled = network_enabled

    def _json(self, url, **kwargs):
        if not self.network_enabled:
            raise ProviderError("NETWORK_DISABLED", "Remote dataset acquisition is disabled")
        if self.client:
            response = self.client.request(kwargs.pop("method", "GET"), url, timeout=20, **kwargs)
        else:
            with httpx.Client(follow_redirects=True, timeout=20) as client:
                response = client.request(kwargs.pop("method", "GET"), url, **kwargs)
        response.raise_for_status()
        return response.json()

    def _path(self, name, identity, suffix):
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
        directory = self.cache / "acquisition"
        directory.mkdir(exist_ok=True)
        return directory / f"{name}-{digest}{suffix}"

    def smap(self, geometry, start, end):
        """Read native cells intersecting the polygon, not a whole global archive."""
        import fsspec
        import numpy as np
        import pyarrow as pa
        import pyarrow.parquet as pq
        import xarray as xr
        from pyproj import Geod

        polygon = shape(geometry)
        identity = {
            "geometry": geometry,
            "start": str(start),
            "end": str(end),
            "source": SMAP_STORE,
        }
        target = self._path("smap", identity, ".parquet")
        if target.exists():
            return register_asset(
                self.cache,
                target,
                "NASA_POWER_SMAP_L4",
                "parquet",
                variable="soil_moisture_timeseries",
                unit="m3/m3",
                version="POWER-SMAP-daily-10.0.0",
                bounds=list(polygon.bounds),
                aggregation_geometry=geometry,
                start=str(start),
                end=str(end),
                depth_cm=[0, 100],
                source_url="https://power.larc.nasa.gov/docs/methodology/data/sources_hydrology/",
            )
        if not self.network_enabled:
            raise ProviderError("NETWORK_DISABLED", "No cached SMAP subset is available")
        ds = xr.open_zarr(fsspec.get_mapper(SMAP_STORE, anon=True), consolidated=True, chunks=None)
        try:
            lat, lon = ds.lat.values, ds.lon.values
            dy, dx = float(abs(np.median(np.diff(lat)))), float(abs(np.median(np.diff(lon))))

            def cell_bounds(values):
                edges = np.concatenate(
                    (
                        [values[0] - (values[1] - values[0]) / 2],
                        (values[:-1] + values[1:]) / 2,
                        [values[-1] + (values[-1] - values[-2]) / 2],
                    )
                )
                return np.minimum(edges[:-1], edges[1:]), np.maximum(edges[:-1], edges[1:])

            lat_low, lat_high = cell_bounds(lat)
            lon_low, lon_high = cell_bounds(lon)
            west, south, east, north = polygon.bounds
            yi = np.flatnonzero((lat_high >= south) & (lat_low <= north))
            xi = np.flatnonzero((lon_high >= west) & (lon_low <= east))
            if not len(yi) or not len(xi) or len(yi) * len(xi) > 64:
                raise ProviderError(
                    "SMAP_WINDOW_TOO_LARGE",
                    "SMAP acquisition requires 1–64 intersecting native cells",
                )
            part = (
                ds[["RZMC", "SFMC"]]
                .isel(lat=yi, lon=xi)
                .sel(time=slice(str(start), str(end)))
                .load()
            )
            if not part.sizes.get("time"):
                raise ProviderError(
                    "SMAP_PERIOD_MISSING", "NASA SMAP store has no records in this period"
                )
            weights = np.zeros((len(yi), len(xi)))
            geod = Geod(ellps="WGS84")
            for a in range(len(yi)):
                for b in range(len(xi)):
                    intersection = polygon.intersection(
                        box(lon_low[xi[b]], lat_low[yi[a]], lon_high[xi[b]], lat_high[yi[a]])
                    )
                    if not intersection.is_empty:
                        weights[a, b] = abs(geod.geometry_area_perimeter(intersection)[0])
            weights = xr.DataArray(
                weights, dims=("lat", "lon"), coords={"lat": part.lat, "lon": part.lon}
            )
            total_weight = float(weights.sum())
            rows = []
            for index, day in enumerate(part.time.values):
                row = {"date": str(day)[:10]}
                for source, name in (("RZMC", "root_zone_moisture"), ("SFMC", "surface_moisture")):
                    values = part[source].isel(time=index)
                    valid = np.isfinite(values) & (values >= 0) & (values <= 1)
                    coverage = float(weights.where(valid, 0).sum()) / max(1, total_weight)
                    row[name] = (
                        float((values.where(valid, 0) * weights).sum() / total_weight)
                        if coverage >= 0.999
                        else None
                    )
                rows.append(row)
            pq.write_table(pa.Table.from_pylist(rows), target, compression="zstd")
            atomic_json(
                target.with_suffix(".json"),
                {
                    **identity,
                    "version": ds.attrs.get("version"),
                    "native_spacing_degrees": [dy, dx],
                    "depths_cm": {"RZMC": [0, 100], "SFMC": [0, 5]},
                    "note": "NASA POWER-distributed SMAP L4 regridded daily product; preserve native spatial support.",
                },
            )
        finally:
            ds.close()
        return self.smap(geometry, start, end)

    def soil(self, geometry, start, end):
        specifications = [
            ("phh2o", "soil_ph", "pH*10"),
            ("sand", "sand_pct", "g/kg"),
            ("silt", "silt_pct", "g/kg"),
            ("clay", "clay_pct", "g/kg"),
        ]

        def fetch(spec):
            prop, variable, unit = spec
            path = SoilGridsWCS(self.cache, self.network_enabled, self.client).coverage(
                geometry, prop
            )
            return register_asset(
                self.cache,
                path,
                "SoilGrids",
                "geotiff",
                variable=variable,
                unit=unit,
                version="2.0",
                depth_cm=[0, 5],
                bounds=list(shape(geometry).bounds),
                source_url="https://docs.isric.org/globaldata/soilgrids/",
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(fetch, specifications))

    def _search(self, collections, geometry, start=None, end=None, limit=8):
        query = {"collections": collections, "intersects": geometry, "limit": limit}
        if start and end:
            query["datetime"] = f"{start}T00:00:00Z/{end}T23:59:59Z"
        return self._json(STAC, method="POST", json=query).get("features", [])

    def _clip(self, href, geometry, label):
        import rasterio
        from rasterio.features import geometry_window
        from rasterio.warp import transform_geom

        target = self._path(label, {"href": href, "geometry": geometry}, ".tif")
        if target.exists():
            return target
        signed = self._json(
            "https://planetarycomputer.microsoft.com/api/sas/v1/sign", params={"href": href}
        )["href"]
        with rasterio.Env(
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            GDAL_HTTP_TIMEOUT=20,
            GDAL_HTTP_MAX_RETRY=1,
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
        ):
            with rasterio.open(signed) as source:
                transformed = transform_geom("EPSG:4326", source.crs, geometry)
                window = geometry_window(source, [transformed], pad_x=2, pad_y=2)
                if window.width * window.height > 2_000_000:
                    raise ProviderError(
                        "REMOTE_WINDOW_TOO_LARGE", "Raster subset exceeds two million native pixels"
                    )
                values = source.read(1, window=window)
                profile = source.profile.copy()
                profile.update(
                    driver="GTiff",
                    height=values.shape[0],
                    width=values.shape[1],
                    count=1,
                    transform=source.window_transform(window),
                    compress="deflate",
                )
                with rasterio.open(target, "w", **profile) as output:
                    output.write(values, 1)
        return target

    def terrain(self, geometry, start, end):
        import rasterio
        from rasterio.merge import merge

        items = self._search(["nasadem"], geometry, limit=8)
        if not items or len(items) > 4:
            raise ProviderError(
                "NASADEM_COVERAGE_REQUIRED", "Need one to four NASADEM tiles for this plot"
            )
        paths = [
            self._clip(item["assets"]["elevation"]["href"], geometry, item["id"]) for item in items
        ]
        target = self._path(
            "nasadem-merged", {"geometry": geometry, "items": [i["id"] for i in items]}, ".tif"
        )
        sources = [rasterio.open(path) for path in paths]
        try:
            values, transform = merge(sources)
            profile = sources[0].profile.copy()
            profile.update(height=values.shape[1], width=values.shape[2], transform=transform)
            with rasterio.open(target, "w", **profile) as output:
                output.write(values)
        finally:
            for source in sources:
                source.close()
        return register_asset(
            self.cache,
            target,
            "NASADEM_HGT",
            "geotiff",
            variable="elevation",
            unit="m",
            bounds=list(shape(geometry).bounds),
            version="001",
            source_url="https://planetarycomputer.microsoft.com/dataset/nasadem",
        )

    def hls(self, geometry, start, end, max_scenes=10):
        # Bound and distribute samples across the period instead of fetching only its newest year.
        months = [
            (y, m)
            for y in range(start.year, end.year + 1)
            for m in range(1, 13)
            if date(y, m, monthrange(y, m)[1]) >= start and date(y, m, 1) <= end
        ]
        step = max(1, len(months) / max_scenes)
        months = [
            months[min(len(months) - 1, int(i * step))] for i in range(min(max_scenes, len(months)))
        ]
        assets = []
        for year, month in months:
            first, last = (
                max(start, date(year, month, 1)),
                min(end, date(year, month, monthrange(year, month)[1])),
            )
            items = self._search(["hls2-l30", "hls2-s30"], geometry, first, last, limit=12)
            items.sort(key=lambda item: item.get("properties", {}).get("eo:cloud_cover", 100))
            for item in items[:3]:
                if not shape(item["geometry"]).covers(shape(geometry)):
                    continue
                bands = item["assets"]
                nir = "B8A" if item.get("collection") == "hls2-s30" else "B05"
                red = self._clip(bands["B04"]["href"], geometry, item["id"] + "-red")
                near = self._clip(bands[nir]["href"], geometry, item["id"] + "-nir")
                quality = self._clip(bands["Fmask"]["href"], geometry, item["id"] + "-mask")
                day = item["properties"]["datetime"][:10]
                assets.append(
                    register_asset(
                        self.cache,
                        red,
                        "HLS_v2",
                        "geotiff",
                        variable="ndvi",
                        unit="index",
                        version="2.0",
                        red_path=red,
                        nir_path=near,
                        quality_path=quality,
                        reflectance_scale=0.0001,
                        bounds=list(shape(geometry).bounds),
                        start=day,
                        end=day,
                        source_url="https://hls.gsfc.nasa.gov/data-access-and-tools/",
                    )
                )
                break
        if not assets:
            raise ProviderError(
                "HLS_NO_COVERAGE", "No covering HLS scenes found in requested sampling months"
            )
        return assets

    def acquire(
        self, geometry, start, end, datasets=("smap", "soil", "terrain", "hls"), max_scenes=10
    ):
        report = {
            "period": [str(start), str(end)],
            "bounds": list(shape(geometry).bounds),
            "sources_preserved": True,
            "datasets": {},
        }

        def run(name):
            try:
                method = getattr(self, name)
                result = (
                    method(geometry, start, end, max_scenes=max_scenes)
                    if name == "hls"
                    else method(geometry, start, end)
                )
                return name, {
                    "status": "available",
                    "assets": result if isinstance(result, list) else [result],
                }
            except Exception as error:
                return name, {
                    "status": "unavailable",
                    "reason": error.code
                    if isinstance(error, ProviderError)
                    else type(error).__name__,
                }

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(run, name) for name in datasets]
            for future in as_completed(futures):
                name, result = future.result()
                report["datasets"][name] = result
        atomic_json(
            self._path(
                "report", {"geometry": geometry, "start": str(start), "end": str(end)}, ".json"
            ),
            report,
        )
        return report
