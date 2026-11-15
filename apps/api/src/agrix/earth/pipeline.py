"""Real data profiles with independent provider failures and reproducible asset identities."""

import hashlib
import json
import math
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from pyproj import Geod
from shapely.geometry import box, shape

from agrix.earth.cache import write_weather
from agrix.earth.manifest import file_hash, load_assets, managed_path
from agrix.earth.providers import SoilGridsWCS
from agrix.earth.rasters import sample_hls_ndvi, sample_netcdf, sample_raster
from agrix.earth.records import ProviderError, WeatherSeries, dates
from agrix.earth.weather import (
    METEOROLOGY_COLUMNS,
    WEATHER_COLUMNS,
    PowerProvider,
    load_giovanni_csv,
    load_power_json,
    numeric,
)
from agrix.models import (
    AssetReference,
    Availability,
    DailyWeather,
    DateRange,
    Evidence,
    LayerCoverage,
    Metric,
    Origin,
    Plot,
    PlotProfile,
    Reason,
    UnitConversion,
)

PROFILE_VERSION = "earth-profile-1.2.0"
LAYER_METRICS = {
    "rainfall": {"rainfall_mm"},
    "temperature": {"temperature_c"},
    "soil_properties": {"soil_ph"},
    "soil_moisture": {"soil_moisture"},
    "terrain": {"terrain_slope"},
    "vegetation": {"ndvi"},
}
DEFAULT_UNITS = {
    "rainfall_mm": "mm",
    "temperature_c": "°C",
    "soil_ph": "pH",
    "soil_moisture": "m3/m3",
    "terrain_slope": "degrees",
    "ndvi": "index",
}
VARIABLE_LAYERS = {
    "soil_ph": "soil_properties",
    "clay_pct": "soil_properties",
    "sand_pct": "soil_properties",
    "silt_pct": "soil_properties",
    "soil_organic_carbon": "soil_properties",
    "soil_bulk_density": "soil_properties",
    "soil_moisture": "soil_moisture",
    "soil_moisture_surface": "soil_moisture",
    "soil_moisture_timeseries": "soil_moisture",
    "terrain_slope": "terrain",
    "elevation": "terrain",
    "ndvi": "vegetation",
}


def _decimal(value):
    return Decimal(str(round(float(value), 8))) if value is not None else None


def _period(rows) -> DateRange | None:
    keys = [date.fromisoformat(str(row["date"])[:10]) for row in rows]
    return DateRange(start=min(keys), end=max(keys)) if keys else None


def _availability(known: int, expected: int):
    return (
        Availability.available
        if known == expected and known
        else (Availability.partial if known else Availability.missing)
    )


def _conversion(unit: str, variable: str) -> tuple[str, float]:
    normalized = unit.lower().replace(" ", "").replace("³", "3")
    if variable == "soil_ph":
        if normalized in {"ph", "1", "dimensionless"}:
            return "pH", 1
        if normalized in {"ph*10", "phx10", "ph×10"}:
            return "pH", 0.1
    if variable in {"clay_pct", "sand_pct", "silt_pct"}:
        if normalized in {"%", "percent"}:
            return "%", 1
        if normalized in {"g/kg", "gkg-1", "permille"}:
            return "%", 0.1
    if variable.startswith("soil_moisture") and normalized in {"m3/m3", "m3m-3", "cm3/cm3"}:
        return "m3/m3", 1
    if variable == "soil_organic_carbon":
        if normalized in {"g/kg", "gkg-1"}:
            return "g/kg", 1
        if normalized in {"dg/kg", "dgkg-1"}:
            return "g/kg", 0.1
    if variable == "soil_bulk_density":
        if normalized in {"kg/m3", "kgm-3"}:
            return "kg/m3", 1
        if normalized in {"cg/cm3"}:
            return "kg/m3", 10
        if normalized in {"g/cm3", "kg/dm3"}:
            return "kg/m3", 1000
    if variable in {"terrain_slope", "ndvi", "elevation"}:
        return DEFAULT_UNITS.get(variable, unit or "m"), 1
    if unit:
        return unit, 1
    raise ProviderError("RASTER_UNIT_MISSING", "An explicit scientific unit is required")


class ProfilePipeline:
    """Extract available evidence for a plot without pretending that missing data exist.

    Network access is limited to public POWER and opt-in SoilGrids WCS. NASA
    protected/global datasets enter through explicit registered subsets. Layer
    progress callbacks are optional and contain no credentials or provider error
    response bodies.
    """

    def __init__(
        self,
        cache_dir: Path,
        network_enabled: bool = True,
        *,
        client=None,
        soilgrids_enabled: bool | None = None,
        progress_callback=None,
        remote_assets_enabled: bool | None = None,
    ):
        self.cache_dir = Path(cache_dir).resolve()
        self.network_enabled = network_enabled
        self.client = client
        self.soilgrids_enabled = (
            os.getenv("AGRIX_SOILGRIDS_WCS", "0") == "1"
            if soilgrids_enabled is None
            else soilgrids_enabled
        )
        self.progress_callback = progress_callback
        self.remote_assets_enabled = (
            os.getenv("AGRIX_REMOTE_ASSETS", "0") == "1"
            if remote_assets_enabled is None
            else remote_assets_enabled
        )

    def _progress(self, layer: LayerCoverage) -> None:
        if self.progress_callback is not None:
            self.progress_callback(layer.model_copy(deep=True))

    def run(
        self,
        plot: Plot,
        start: date,
        end: date,
        previous_profile: PlotProfile | None = None,
        retry_layers: list[str] | None = None,
    ) -> PlotProfile:
        dates(start, end)
        if previous_profile and (
            previous_profile.plot_id != plot.id or previous_profile.plot_revision != plot.revision
        ):
            raise ValueError("Previous profile must match the plot and revision")
        if previous_profile and retry_layers and previous_profile.weather:
            if {day.date for day in previous_profile.weather} != set(dates(start, end)):
                raise ValueError("A targeted retry must preserve the previous weather period")
        unknown_layers = set(retry_layers or []) - set(LAYER_METRICS)
        if unknown_layers:
            raise ValueError("Unknown retry layers: " + ", ".join(sorted(unknown_layers)))
        geometry = plot.geometry.model_dump(mode="json")
        metrics = {
            key: value.model_copy(deep=True)
            for key, value in (previous_profile.metrics.items() if previous_profile else [])
        }
        evidence = {
            item.id: item.model_copy(deep=True)
            for item in (previous_profile.evidence if previous_profile else [])
        }
        references = {
            item.id: item.model_copy(deep=True)
            for item in (previous_profile.assets if previous_profile else [])
        }
        layers = {
            item.id: item.model_copy(deep=True)
            for item in (previous_profile.layers if previous_profile else [])
        }
        weather = (
            [day.model_copy(deep=True) for day in previous_profile.weather]
            if previous_profile
            else []
        )
        selected = set(retry_layers) if retry_layers and previous_profile else set(LAYER_METRICS)
        # Reprocessing replaces that layer's metrics rather than retaining stale
        # values from an asset removed or invalidated since the previous attempt.
        for key in list(metrics):
            base = next(
                (
                    name
                    for name in VARIABLE_LAYERS
                    if key == name or key.startswith(name + "_depth_")
                ),
                key,
            )
            layer_id = VARIABLE_LAYERS.get(
                base,
                "rainfall"
                if base == "rainfall_mm"
                else "temperature"
                if base == "temperature_c"
                else None,
            )
            if layer_id in selected:
                del metrics[key]
        try:
            if self.remote_assets_enabled and self.network_enabled:
                from agrix.earth.acquisition import DataAcquirer

                remote = [
                    name
                    for name, layer in (
                        ("smap", "soil_moisture"),
                        ("soil", "soil_properties"),
                        ("terrain", "terrain"),
                        ("hls", "vegetation"),
                    )
                    if layer in selected
                ]
                DataAcquirer(self.cache_dir, client=self.client).acquire(
                    geometry,
                    start,
                    end,
                    remote,
                    max_scenes=int(os.getenv("AGRIX_HLS_MAX_SCENES", "10")),
                )
            assets = load_assets(self.cache_dir)
            manifest_error = None
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            assets = []
            manifest_error = ProviderError("ASSET_MANIFEST_INVALID", str(exc))
        for layer_id in selected & {"temperature", "rainfall"}:
            self._progress(
                LayerCoverage(
                    id=layer_id,
                    availability=Availability.missing,
                    evidence_ids=[],
                    state="running",
                    attempts=1,
                    updated_at=datetime.now(timezone.utc),
                )
            )
        if selected & {"temperature", "rainfall"}:
            incoming_weather, weather_metrics, weather_evidence, weather_refs, weather_layers = (
                self._weather(plot, start, end, assets, selected & {"temperature", "rainfall"})
            )
            selected_weather = selected & {"temperature", "rainfall"}
            if previous_profile and selected_weather != {"temperature", "rainfall"}:
                weather = self._merge_weather(
                    weather,
                    incoming_weather,
                    selected_weather,
                    previous_profile.metrics,
                    weather_metrics,
                    start,
                    end,
                )
                selected_ids = {
                    eid
                    for key in ("rainfall_mm", "temperature_c")
                    if ("rainfall" if key == "rainfall_mm" else "temperature") in selected_weather
                    for eid in weather_metrics[key].evidence_ids
                }
                weather_evidence = [item for item in weather_evidence if item.id in selected_ids]
                weather_refs = [
                    item for item in weather_refs if item.dataset_id != "processed_daily_weather"
                ]
                if weather:
                    weather_refs.append(self._cache_weather(plot, start, end, weather))
            else:
                weather = incoming_weather
            for key, value in weather_metrics.items():
                if (
                    VARIABLE_LAYERS.get(key, "rainfall" if key == "rainfall_mm" else "temperature")
                    in selected
                ):
                    metrics[key] = value
            evidence.update({item.id: item for item in weather_evidence})
            references.update({item.id: item for item in weather_refs})
            for layer in weather_layers:
                if layer.id in selected:
                    layers[layer.id] = layer
                    self._progress(layer)
        for layer_id in ("soil_properties", "soil_moisture", "terrain", "vegetation"):
            if layer_id not in selected:
                continue
            self._progress(
                LayerCoverage(
                    id=layer_id,
                    availability=Availability.missing,
                    evidence_ids=[],
                    state="running",
                    attempts=1,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            matching = [
                item
                for item in assets
                if VARIABLE_LAYERS.get(item.get("variable")) == layer_id
                and (not item.get("bounds") or box(*item["bounds"]).intersects(shape(geometry)))
                and (
                    not item.get("aggregation_geometry")
                    or shape(item["aggregation_geometry"]).equals(shape(geometry))
                )
            ]
            if layer_id == "soil_properties" and not matching and self.soilgrids_enabled:
                try:
                    path = SoilGridsWCS(self.cache_dir, self.network_enabled, self.client).coverage(
                        geometry
                    )
                    matching = [
                        {
                            "id": "soilgrids-phh2o-0-5cm-" + file_hash(path)[:16],
                            "dataset_id": "SoilGrids",
                            "version": "2.0",
                            "format": "geotiff",
                            "path": str(path),
                            "variable": "soil_ph",
                            "unit": "pH",
                            "original_unit": "pH*10",
                            "scale": 0.1,
                            "depth_cm": [0, 5],
                            "source_url": "https://docs.isric.org/globaldata/soilgrids/SoilGrids_faqs.html",
                        }
                    ]
                except (ProviderError, OSError) as exc:
                    manifest_error_for_layer = (
                        exc
                        if isinstance(exc, ProviderError)
                        else ProviderError(
                            "SOILGRIDS_REQUEST_FAILED", "Could not read SoilGrids subset"
                        )
                    )
                    matching = []
                else:
                    manifest_error_for_layer = manifest_error
            else:
                manifest_error_for_layer = manifest_error
            layer_metrics = []
            failures = []
            for asset in matching:
                if asset.get("start") and str(asset["start"])[:10] > end.isoformat():
                    continue
                if asset.get("end") and str(asset["end"])[:10] < start.isoformat():
                    continue
                try:
                    if asset.get("variable") == "soil_moisture_timeseries":
                        source, reference, measured, weather = self._smap_timeseries(
                            asset, geometry, start, end, weather
                        )
                        for key, metric in measured.items():
                            metrics[key] = metric
                            layer_metrics.append(metric)
                        evidence[source.id] = source
                        references[reference.id] = reference
                        continue
                    key, metric, source, reference = self._raster(asset, geometry, start, end)
                    if metric.depth_cm and layer_id == "soil_properties":
                        low, high = metric.depth_cm
                        metrics[f"{key}_depth_{low}_{high}_cm"] = metric
                        prior = metrics.get(key)
                        # Preserve every depth separately. The shallowest supplied
                        # interval remains the primary context; no depth-weighted
                        # pH averaging is scientifically implied.
                        if (
                            prior is None
                            or prior.depth_cm is None
                            or metric.depth_cm < prior.depth_cm
                        ):
                            metrics[key] = metric
                    else:
                        metrics[key] = metric
                    evidence[source.id] = source
                    references[reference.id] = reference
                    for companion in ("red_path", "nir_path", "quality_path"):
                        if asset.get(companion):
                            companion_path = managed_path(self.cache_dir, asset[companion])
                            companion_hash = file_hash(companion_path)
                            companion_id = "raw-" + companion_hash[:24]
                            references[companion_id] = AssetReference(
                                id=companion_id,
                                dataset_id=asset["dataset_id"],
                                path=str(companion_path),
                                sha256=companion_hash,
                                format="geotiff",
                                period=reference.period,
                                variables=[companion.removesuffix("_path")],
                            )
                    layer_metrics.append(metric)
                except (
                    ProviderError,
                    ValueError,
                    OSError,
                    KeyError,
                    ImportError,
                    RuntimeError,
                ) as exc:
                    if isinstance(exc, ProviderError) and exc.code in {
                        "RASTER_NO_COVERAGE",
                        "SMAP_FOOTPRINT_MISMATCH",
                    }:
                        continue
                    failures.append(
                        exc
                        if isinstance(exc, ProviderError)
                        else ProviderError("RASTER_PROCESSING_FAILED", str(exc))
                    )
            if not layer_metrics:
                error = (
                    failures[0]
                    if failures
                    else (
                        manifest_error_for_layer
                        or ProviderError(
                            "LOCAL_ASSET_REQUIRED",
                            f"No registered {layer_id} subset covers the plot and requested period",
                        )
                    )
                )
                for key in LAYER_METRICS[layer_id]:
                    source, metric = self._missing(plot, layer_id, key, error)
                    metrics[key] = metric
                    evidence[source.id] = source
                layer = LayerCoverage(
                    id=layer_id,
                    availability=Availability.missing,
                    evidence_ids=[metrics[key].evidence_ids[0] for key in LAYER_METRICS[layer_id]],
                    reason=Reason(code=error.code, values={"detail": str(error)}),
                    state="unsupported" if error.code == "LOCAL_ASSET_REQUIRED" else "failed",
                    attempts=1,
                    updated_at=datetime.now(timezone.utc),
                )
            else:
                for key in LAYER_METRICS[layer_id]:
                    if key not in metrics or metrics[key].value is None:
                        error = ProviderError(
                            "REQUIRED_VARIABLE_MISSING", f"Layer lacks {key} at the required depth"
                        )
                        source, metrics[key] = self._missing(plot, layer_id, key, error)
                        evidence[source.id] = source
                complete = all(m.availability == Availability.available for m in layer_metrics)
                complete = (
                    complete
                    and not failures
                    and all(metrics[k].value is not None for k in LAYER_METRICS[layer_id])
                )
                layer = LayerCoverage(
                    id=layer_id,
                    availability=Availability.available if complete else Availability.partial,
                    evidence_ids=sorted(
                        {
                            eid
                            for m in layer_metrics + [metrics[k] for k in LAYER_METRICS[layer_id]]
                            for eid in m.evidence_ids
                        }
                    ),
                    state="complete" if complete else "partial",
                    attempts=1,
                    reason=None if complete else Reason(code="LAYER_PARTIAL_COVERAGE"),
                    updated_at=datetime.now(timezone.utc),
                )
            layers[layer_id] = layer
            self._progress(layer)
        # Retry of a single layer preserves every completed independent layer.
        for layer_id, keys in LAYER_METRICS.items():
            for key in keys:
                if key not in metrics:
                    source, metrics[key] = self._missing(
                        plot,
                        layer_id,
                        key,
                        ProviderError(
                            "LAYER_NOT_ANALYZED", "Layer was not included in this analysis"
                        ),
                    )
                    evidence[source.id] = source
            if layer_id not in layers:
                layers[layer_id] = LayerCoverage(
                    id=layer_id,
                    availability=Availability.missing,
                    evidence_ids=metrics[next(iter(keys))].evidence_ids,
                    state="pending",
                )
        if weather:
            combined_reference = self._cache_weather(plot, start, end, weather)
            references[combined_reference.id] = combined_reference
        return PlotProfile(
            plot_id=plot.id,
            plot_revision=plot.revision,
            fixture_version=PROFILE_VERSION,
            zone_id=None,
            rule_coverage=Availability.unsupported,
            metrics=metrics,
            layers=list(layers.values()),
            evidence=list(evidence.values()),
            weather=weather,
            assets=list(references.values()),
            synthetic=False,
            water_availability={
                "source": "NASA precipitation and SMAP root-zone moisture",
                "root_zone_days": sum(row.root_zone_moisture is not None for row in weather),
                "rain_days": sum(row.rain_mm is not None for row in weather),
                "requested_days": len(dates(start, end)),
                "irrigation_access": "unverified",
                "note": "Modeled crop water is separate from verified irrigation delivery, well yield and legal allocation.",
            },
        )

    def _smap_timeseries(self, asset, geometry, start, end, weather):
        import pyarrow.parquet as pq

        if not shape(asset.get("aggregation_geometry", {})).equals(shape(geometry)):
            raise ProviderError(
                "SMAP_FOOTPRINT_MISMATCH", "SMAP aggregation belongs to another polygon"
            )
        path = managed_path(self.cache_dir, asset["path"])
        rows = {
            str(row["date"])[:10]: row
            for row in pq.read_table(path).to_pylist()
            if str(start) <= str(row["date"])[:10] <= str(end)
        }
        expected = dates(start, end)
        eid = "eo-smap-" + file_hash(path)[:20]
        measured = {}
        for column, key, depth in (
            ("root_zone_moisture", "soil_moisture", (0, 100)),
            ("surface_moisture", "soil_moisture_surface", (0, 5)),
        ):
            values = [
                float(r[column])
                for r in rows.values()
                if r.get(column) is not None and 0 <= float(r[column]) <= 1
            ]
            status = _availability(len(values), len(expected))
            measured[key] = Metric(
                value=_decimal(sum(values) / len(values)) if values else None,
                unit="m3/m3",
                availability=status,
                origin=Origin.modeled,
                evidence_ids=[eid],
                depth_cm=depth,
                reason=None
                if status == Availability.available
                else Reason(code="SMAP_DAILY_COVERAGE_PARTIAL"),
            )
        previous = {row.date: row for row in weather}
        combined = []
        for day in expected:
            item = previous.get(day, DailyWeather(date=day))
            observation = rows.get(str(day), {})
            updates = {
                key: _decimal(observation.get(key))
                for key in ("root_zone_moisture", "surface_moisture")
            }
            combined.append(
                item.model_copy(
                    update={
                        **updates,
                        "evidence_ids": list(dict.fromkeys(item.evidence_ids + [eid])),
                    }
                )
            )
        source = Evidence(
            id=eid,
            source_id=asset["dataset_id"],
            source_version=asset["version"],
            origin=Origin.modeled,
            synthetic=False,
            period=DateRange(start=start, end=end),
            resolution_m=9000,
            geographic_bounds=tuple(shape(geometry).bounds),
            depth_cm=(0, 100),
            availability=measured["soil_moisture"].availability,
            source_url=asset.get("source_url"),
            quality_notes=[
                "NASA POWER-distributed SMAP L4 daily archive; native ~9 km information, regridded coordinates.",
                "Intersecting cells are geodesically area weighted; missing cells remain missing.",
                "Root zone is 0–100 cm; surface 0–5 cm is separate. NASA store variable metadata is marked reviewing.",
            ],
            uncertainty="Regional assimilated/model product; local accuracy unvalidated.",
        )
        reference = AssetReference(
            id=asset["id"],
            dataset_id=asset["dataset_id"],
            path=str(path),
            sha256=file_hash(path),
            format="parquet",
            period=DateRange(start=start, end=end),
            variables=["root_zone_moisture", "surface_moisture"],
        )
        return source, reference, measured, combined

    @staticmethod
    def _merge_weather(previous, incoming, selected, old_metrics, new_metrics, start, end):
        """Replace only selected daily variables and their per-variable lineage."""
        previous_by_day = {row.date: row for row in previous}
        incoming_by_day = {row.date: row for row in incoming}
        selected_keys = {
            "rainfall_mm" if layer == "rainfall" else "temperature_c" for layer in selected
        }
        old_selected_ids = {
            eid
            for key in selected_keys
            if key in old_metrics
            for eid in old_metrics[key].evidence_ids
        }
        old_retained_ids = {
            eid
            for key in {"rainfall_mm", "temperature_c"} - selected_keys
            if key in old_metrics
            for eid in old_metrics[key].evidence_ids
        }
        new_selected_ids = {eid for key in selected_keys for eid in new_metrics[key].evidence_ids}
        columns = (["rain_mm"] if "rainfall" in selected else []) + (
            list(METEOROLOGY_COLUMNS) if "temperature" in selected else []
        )
        merged = []
        for day in dates(start, end):
            prior = previous_by_day.get(day, DailyWeather(date=day))
            replacement = incoming_by_day.get(day, DailyWeather(date=day))
            retained_ids = [
                eid
                for eid in prior.evidence_ids
                if eid not in old_selected_ids or eid in old_retained_ids
            ]
            selected_ids = [eid for eid in replacement.evidence_ids if eid in new_selected_ids]
            merged.append(
                prior.model_copy(
                    update={
                        **{column: getattr(replacement, column) for column in columns},
                        "evidence_ids": list(dict.fromkeys(retained_ids + selected_ids)),
                    }
                )
            )
        return merged

    def _cache_weather(self, plot, start, end, weather):
        rows = [day.model_dump(mode="json") for day in weather]
        for row in rows:
            for name in WEATHER_COLUMNS:
                row[name] = float(row[name]) if row[name] is not None else None
        identity = {
            "version": PROFILE_VERSION,
            "period": [start.isoformat(), end.isoformat()],
            "geometry": plot.geometry.model_dump(mode="json"),
            "evidence_ids": sorted({eid for day in weather for eid in day.evidence_ids}),
        }
        path, digest, format = write_weather(self.cache_dir, rows, identity)
        return AssetReference(
            id="weather-" + digest[:24],
            dataset_id="processed_daily_weather",
            path=str(path),
            sha256=digest,
            format=format,
            period=DateRange(start=start, end=end),
            variables=list(WEATHER_COLUMNS),
        )

    def _missing(self, plot, layer_id, metric_id, error):
        eid = f"missing-{metric_id}-{hashlib.sha256(error.code.encode()).hexdigest()[:10]}"
        reason = Reason(code=error.code, values={"detail": str(error), "metric": metric_id})
        source = Evidence(
            id=eid,
            source_id=layer_id,
            source_version=PROFILE_VERSION,
            origin=Origin.modeled,
            availability=Availability.missing,
            synthetic=False,
            geographic_bounds=tuple(shape(plot.geometry.model_dump()).bounds),
            reason=reason,
            quality_notes=["No value was inferred from missing provider evidence."],
        )
        return source, Metric(
            value=None,
            unit=DEFAULT_UNITS.get(metric_id, "unknown"),
            availability=Availability.missing,
            origin=Origin.modeled,
            evidence_ids=[eid],
            reason=reason,
        )

    def _local_series(self, assets, polygon, start, end):
        series = []
        failures = []
        for asset in assets:
            format = asset["format"]
            try:
                path = managed_path(self.cache_dir, asset["path"])
                if format == "power_json":
                    item = load_power_json(path)
                elif format == "giovanni_csv":
                    item = load_giovanni_csv(path)
                    if (
                        asset["dataset_id"] in {"GPM_3IMERGDF", "GPM_3IMERGDL", "GPM_3IMERGDE"}
                        and asset["dataset_id"] != item.dataset_id
                    ):
                        raise ProviderError(
                            "PRODUCT_ID_MISMATCH",
                            "Registered IMERG run contradicts the file's source identity",
                        )
                elif format == "parquet" and asset.get("variable") == "weather":
                    import pyarrow.parquet as pq

                    rows = pq.read_table(path).to_pylist()
                    if not asset.get("bounds"):
                        raise ProviderError(
                            "MISSING_SPATIAL_LINEAGE",
                            "Imported series requires its aggregation bounds",
                        )
                    for row in rows:
                        row["date"] = str(row["date"])[:10]
                        for column in WEATHER_COLUMNS:
                            row[column] = numeric(row.get(column))
                        if row["rain_mm"] is not None and row["rain_mm"] < 0:
                            row["rain_mm"] = None
                    if len({row["date"] for row in rows}) != len(rows):
                        raise ProviderError(
                            "INVALID_DAILY_SERIES", "Imported daily records must have unique dates"
                        )
                    item = WeatherSeries(
                        asset["dataset_id"],
                        asset["version"],
                        asset.get("source_url", ""),
                        rows,
                        tuple(asset["bounds"]),
                        path=path,
                    )
                elif format in {"geotiff", "netcdf"} and asset.get("variable") == "rainfall_mm":
                    if (
                        not asset.get("start")
                        or str(asset.get("end", asset["start"]))[:10] != str(asset["start"])[:10]
                    ):
                        continue  # A single raster cannot be broadcast over a multi-day period.
                    if format == "geotiff":
                        result = sample_raster(
                            path,
                            polygon.__geo_interface__,
                            band=asset.get("band", 1),
                            scale=asset.get("scale"),
                            offset=asset.get("offset"),
                        )
                    else:
                        result = sample_netcdf(
                            path,
                            polygon.__geo_interface__,
                            asset.get("data_variable", "precipitation"),
                            day=date.fromisoformat(str(asset["start"])[:10]),
                        )
                    unit = str(asset.get("unit", ""))
                    factor = (
                        1000 if unit in {"m", "m/day"} else 1 if unit in {"mm", "mm/day"} else None
                    )
                    if factor is None:
                        raise ProviderError(
                            "UNSUPPORTED_PROVIDER_UNIT",
                            "Daily rainfall raster requires mm or m daily units",
                        )
                    item = WeatherSeries(
                        asset["dataset_id"],
                        asset["version"],
                        asset.get("source_url", ""),
                        [
                            {
                                "date": str(asset["start"])[:10],
                                "rain_mm": result.mean * factor
                                if result.mean is not None
                                else None,
                            }
                        ],
                        tuple(polygon.bounds),
                        units={"rain_mm": unit},
                        notes=result.notes,
                        path=path,
                    )
                else:
                    continue
                if not box(*asset.get("bounds", item.bounds)).covers(polygon):
                    continue
                item.rows = [
                    row
                    for row in item.rows
                    if start.isoformat() <= str(row["date"])[:10] <= end.isoformat()
                ]
                if item.rows:
                    series.append(item)
            except (ProviderError, ValueError, OSError, KeyError, ImportError, RuntimeError) as exc:
                # The caller still gets live POWER or an explicit missing layer; a
                # malformed optional source cannot poison other independent inputs.
                failures.append(
                    exc
                    if isinstance(exc, ProviderError)
                    else ProviderError(
                        "LOCAL_WEATHER_ASSET_INVALID",
                        "A registered weather input could not be decoded",
                    )
                )
                continue
        return self._combine_series(series), failures

    @staticmethod
    def _combine_series(series):
        groups = {}
        for item in series:
            key = (item.dataset_id, item.version, item.bounds)
            groups.setdefault(key, []).append(item)
        result = []
        for items in groups.values():
            first = items[0]
            rows = {}
            for item in items:
                for row in item.rows:
                    prior = rows.get(row["date"], {})
                    # Same product/version/footprint can contribute optional columns.
                    # A sparse re-export must not erase available humidity/radiation.
                    rows[row["date"]] = {
                        **prior,
                        **{
                            key: value
                            for key, value in row.items()
                            if value is not None or prior.get(key) is None
                        },
                    }
            paths = [str(item.path) for item in items if item.path]
            combined = WeatherSeries(
                first.dataset_id,
                first.version,
                first.source_url,
                [rows[key] for key in sorted(rows)],
                first.bounds,
                first.units,
                list(dict.fromkeys(note for item in items for note in item.notes)),
                first.path,
            )
            combined._source_paths = paths
            result.append(combined)
        return result

    def _power_for_polygon(self, polygon, start, end):
        min_lon, min_lat, max_lon, max_lat = polygon.bounds
        x_indices = range(
            math.floor((min_lon - 0.3125) / 0.625), math.ceil((max_lon + 0.3125) / 0.625) + 1
        )
        y_indices = range(math.floor((min_lat - 0.25) / 0.5), math.ceil((max_lat + 0.25) / 0.5) + 1)
        cells = []
        for i in x_indices:
            for j in y_indices:
                lon, lat = i * 0.625, j * 0.5
                intersection = polygon.intersection(
                    box(lon - 0.3125, lat - 0.25, lon + 0.3125, lat + 0.25)
                )
                if not intersection.is_empty and intersection.area > 0:
                    cells.append(
                        (
                            lon,
                            lat,
                            abs(Geod(ellps="WGS84").geometry_area_perimeter(intersection)[0]),
                        )
                    )
        if len(cells) > 16:
            raise ProviderError(
                "POWER_SPATIAL_WINDOW_TOO_LARGE", "Plot spans more than 16 meteorological cells"
            )
        provider = PowerProvider(self.cache_dir, self.network_enabled, self.client)
        sources = [(provider.fetch(lon, lat, start, end), weight) for lon, lat, weight in cells]
        if not sources:
            raise ProviderError("POWER_NO_COVERAGE", "No weather grid cell intersects the plot")
        rows = []
        by_source = [({r["date"]: r for r in series.rows}, weight) for series, weight in sources]
        total_weight = sum(weight for _, weight in by_source)
        for day in dates(start, end):
            key = day.isoformat()
            row = {"date": key}
            for variable in WEATHER_COLUMNS:
                usable = [
                    (numeric(data.get(key, {}).get(variable)), weight) for data, weight in by_source
                ]
                # An absent cell leaves the day's area-average unknown, rather than
                # renormalizing onto an unrepresentative surviving portion.
                row[variable] = (
                    sum(value * weight for value, weight in usable) / total_weight
                    if all(value is not None for value, _ in usable)
                    else None
                )
            rows.append(row)
        first = sources[0][0]
        result = WeatherSeries(
            first.dataset_id,
            first.version,
            first.source_url,
            rows,
            tuple(polygon.bounds),
            first.units,
            first.notes
            + [
                "Daily values use intersecting native meteorological cells, weighted by plot footprint."
            ],
            first.path,
        )
        result._source_paths = [str(item.path) for item, _ in sources if item.path]
        return result

    def _weather(self, plot, start, end, assets, required_layers=None):
        polygon = shape(plot.geometry.model_dump(mode="json"))
        requested = dates(start, end)
        count = len(requested)
        series, errors = self._local_series(assets, polygon, start, end)
        required_layers = required_layers or {"rainfall", "temperature"}
        temperature_complete = any(
            sum(
                all(row.get(name) is not None for name in ("tmin_c", "tmax_c", "tmean_c"))
                for row in item.rows
            )
            == count
            for item in series
        )
        rainfall_complete = any(
            sum(row.get("rain_mm") is not None for row in item.rows) == count for item in series
        )
        if ("temperature" in required_layers and not temperature_complete) or (
            "rainfall" in required_layers and not rainfall_complete
        ):
            try:
                series.append(self._power_for_polygon(polygon, start, end))
            except (ProviderError, ValueError, OSError) as exc:
                errors.append(
                    exc
                    if isinstance(exc, ProviderError)
                    else ProviderError("POWER_REQUEST_FAILED", str(exc))
                )
        temperatures = [
            item for item in series if any(row.get("tmean_c") is not None for row in item.rows)
        ]
        temperature = max(
            temperatures,
            key=lambda item: (
                sum(row.get("tmean_c") is not None for row in item.rows),
                sum(
                    row.get(key) is not None
                    for row in item.rows
                    for key in ("humidity_pct", "wind_m_s", "solar_mj_m2")
                ),
            ),
            default=None,
        )
        rain_sources = [
            item for item in series if any(row.get("rain_mm") is not None for row in item.rows)
        ]
        priority = {"GPM_3IMERGDF": 0, "GPM_3IMERGDL": 1, "NASA_POWER_DAILY": 2, "GPM_3IMERGDE": 3}
        # Prefer complete homogeneous products over silently splicing rainfall runs.
        rain = min(
            rain_sources,
            key=lambda item: (
                sum(row.get("rain_mm") is not None for row in item.rows) < count,
                priority.get(item.dataset_id, 4),
                -sum(row.get("rain_mm") is not None for row in item.rows),
            ),
            default=None,
        )
        records, metrics, layers, references = [], {}, [], []
        source_ids = {}
        for metric_id, source, column, layer_id, unit in (
            ("rainfall_mm", rain, "rain_mm", "rainfall", "mm"),
            ("temperature_c", temperature, "tmean_c", "temperature", "°C"),
        ):
            if source is None:
                error = (
                    errors[0]
                    if errors
                    else ProviderError(
                        "WEATHER_DATA_MISSING",
                        "No registered or cached daily source covers the plot",
                    )
                )
                evidence, metric = self._missing(plot, layer_id, metric_id, error)
                metric.value = None
                layers.append(
                    LayerCoverage(
                        id=layer_id,
                        availability=Availability.missing,
                        evidence_ids=[evidence.id],
                        reason=metric.reason,
                        state="failed",
                        attempts=1,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
            else:
                values = [row[column] for row in source.rows if row.get(column) is not None]
                availability = _availability(len(values), count)
                complete_temperature_days = sum(
                    all(row.get(name) is not None for name in ("tmin_c", "tmax_c", "tmean_c"))
                    for row in source.rows
                )
                if layer_id == "temperature" and complete_temperature_days < count:
                    availability = Availability.partial
                signatures = [
                    file_hash(Path(path))
                    for path in getattr(source, "_source_paths", [str(source.path)])
                    if path and Path(path).is_file()
                ]
                identity = hashlib.sha256(
                    (source.dataset_id + source.version + ":".join(signatures) + metric_id).encode()
                ).hexdigest()[:20]
                eid = "eo-" + identity
                source_ids[layer_id] = eid
                notes = list(source.notes)
                if errors:
                    notes.append(
                        "Some optional registered/provider inputs were unavailable: "
                        + ", ".join(sorted({error.code for error in errors}))
                    )
                if layer_id == "rainfall" and source.dataset_id != "GPM_3IMERGDF":
                    notes.append(
                        f"Fallback rainfall source {source.dataset_id}; no complete IMERG Final baseline available."
                    )
                reason = (
                    None
                    if availability == Availability.available
                    else Reason(
                        code="DAILY_COVERAGE_PARTIAL",
                        values={
                            "valid_days": len(values),
                            "requested_days": count,
                            **(
                                {"complete_min_mean_max_days": complete_temperature_days}
                                if layer_id == "temperature"
                                else {}
                            ),
                        },
                    )
                )
                evidence = Evidence(
                    id=eid,
                    source_id=source.dataset_id,
                    source_version=source.version,
                    origin=Origin.modeled,
                    availability=availability,
                    synthetic=False,
                    source_url=source.source_url,
                    quality_notes=notes,
                    period=_period(source.rows),
                    geographic_bounds=source.bounds,
                    resolution_m=55000 if source.dataset_id == "NASA_POWER_DAILY" else 10000,
                    reason=reason,
                    uncertainty="Regional/model or satellite estimate; local accuracy unvalidated.",
                )
                original_unit = source.units.get(
                    "PRECTOTCORR" if layer_id == "rainfall" else "T2M",
                    source.units.get(column, "mm/day" if layer_id == "rainfall" else "C"),
                )
                normalized_unit = original_unit.lower().replace(" ", "").replace("°", "")
                evidence.unit_conversion = UnitConversion(
                    original_unit=original_unit,
                    canonical_unit="mm/day" if layer_id == "rainfall" else "°C",
                    multiplier=Decimal(
                        1000
                        if layer_id == "rainfall" and normalized_unit in {"m", "m/day", "m/d"}
                        else 1
                    ),
                    offset=Decimal("-273.15")
                    if layer_id == "temperature" and normalized_unit in {"k", "kelvin"}
                    else Decimal(0),
                )
                for source_path in getattr(source, "_source_paths", [str(source.path)]):
                    path = Path(source_path) if source_path else None
                    if path is None or not path.is_file():
                        continue
                    digest = file_hash(path)
                    ref_id = "raw-" + digest[:24]
                    if not any(ref.id == ref_id for ref in references):
                        references.append(
                            AssetReference(
                                id=ref_id,
                                dataset_id=source.dataset_id,
                                path=str(path),
                                sha256=digest,
                                format=path.suffix.lstrip("."),
                                period=_period(source.rows),
                                variables=sorted(source.units) or [column],
                            )
                        )
                value = sum(values) if layer_id == "rainfall" else sum(values) / len(values)
                metric = Metric(
                    value=_decimal(value),
                    unit=unit,
                    availability=availability,
                    origin=Origin.modeled,
                    evidence_ids=[eid],
                    reason=reason,
                )
                layers.append(
                    LayerCoverage(
                        id=layer_id,
                        availability=availability,
                        evidence_ids=[eid],
                        reason=reason,
                        state="complete" if availability == Availability.available else "partial",
                        attempts=1,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
            records.append(evidence)
            metrics[metric_id] = metric
        by_temperature = {row["date"]: row for row in temperature.rows} if temperature else {}
        by_rain = {row["date"]: row for row in rain.rows} if rain else {}
        weather = []
        for day in requested:
            key = day.isoformat()
            values = {
                "rain_mm": by_rain.get(key, {}).get("rain_mm"),
                **{name: by_temperature.get(key, {}).get(name) for name in METEOROLOGY_COLUMNS},
            }
            ids = [
                source_ids[k]
                for k in ("rainfall", "temperature")
                if k in source_ids
                and (
                    values["rain_mm"] is not None
                    if k == "rainfall"
                    else any(values[n] is not None for n in ("tmin_c", "tmax_c", "tmean_c"))
                )
            ]
            weather.append(
                DailyWeather(
                    date=day, **{k: _decimal(v) for k, v in values.items()}, evidence_ids=ids
                )
            )
        if not any(
            any(
                getattr(day, name) is not None
                for name in ("rain_mm", "tmin_c", "tmax_c", "tmean_c")
            )
            for day in weather
        ):
            weather = []
        if weather:
            rows = [day.model_dump(mode="json") for day in weather]
            for row in rows:
                for name in WEATHER_COLUMNS:
                    row[name] = float(row[name]) if row[name] is not None else None
            identity = {
                "version": PROFILE_VERSION,
                "period": [start.isoformat(), end.isoformat()],
                "geometry": plot.geometry.model_dump(mode="json"),
                "evidence_ids": sorted(source_ids.values()),
            }
            path, digest, format = write_weather(self.cache_dir, rows, identity)
            references.append(
                AssetReference(
                    id="weather-" + digest[:24],
                    dataset_id="processed_daily_weather",
                    path=str(path),
                    sha256=digest,
                    format=format,
                    period=DateRange(start=start, end=end),
                    variables=list(WEATHER_COLUMNS),
                )
            )
        return weather, metrics, records, references, layers

    def _raster(self, asset, geometry, start, end):
        path = managed_path(self.cache_dir, asset["path"])
        variable = asset["variable"]
        key = variable
        depth = tuple(asset["depth_cm"]) if asset.get("depth_cm") else None
        if variable == "soil_moisture":
            if depth != (0, 100):
                key = "soil_moisture_surface" if depth == (0, 5) else "soil_moisture_other_depth"
        if variable == "ndvi" and all(
            asset.get(k) for k in ("red_path", "nir_path", "quality_path")
        ):
            summary = sample_hls_ndvi(
                *(
                    managed_path(self.cache_dir, asset[k])
                    for k in ("red_path", "nir_path", "quality_path")
                ),
                geometry,
                reflectance_scale=asset.get("reflectance_scale", 0.0001),
            )
        elif asset["format"] == "netcdf":
            summary = sample_netcdf(
                path,
                geometry,
                asset.get("data_variable", variable),
                start=start,
                end=end,
                latitude_name=asset.get("latitude_name", "lat"),
                longitude_name=asset.get("longitude_name", "lon"),
            )
        else:
            summary = sample_raster(
                path,
                geometry,
                band=asset.get("band", 1),
                scale=asset.get("scale"),
                offset=asset.get("offset"),
                calculation="slope" if variable == "elevation" else "mean",
                source_unit=asset.get("unit"),
                quality_path=managed_path(self.cache_dir, asset["quality_path"])
                if asset.get("quality_path")
                else None,
                quality_valid_values=asset.get("quality_valid_values"),
                quality_invalid_bits=asset.get("quality_invalid_bits"),
            )
            if variable == "elevation":
                key = "terrain_slope"
        if summary.mean is None:
            raise ProviderError(
                "NO_VALID_NATIVE_PIXELS", "Nodata or quality filters leave no valid plot pixels"
            )
        canonical, factor = _conversion(
            str(asset.get("canonical_unit") or asset.get("unit") or ""), key
        )
        value = summary.mean * factor
        if key == "soil_ph" and not 0 <= value <= 14:
            raise ProviderError(
                "SOIL_PH_RANGE_INVALID", "Soil pH requires a valid scientific scale from 0 to 14"
            )
        if (
            key == "soil_ph"
            and str(asset["dataset_id"]).lower().startswith("soilgrids")
            and value == 0
        ):
            raise ProviderError(
                "SOILGRIDS_EMPTY_RESPONSE",
                "Zero SoilGrids pH padding is missing data, not a measured soil pH",
            )
        if key in {"clay_pct", "sand_pct", "silt_pct"} and not 0 <= value <= 100:
            raise ProviderError(
                "SOIL_TEXTURE_RANGE_INVALID", "Soil texture percentages must be between 0 and 100"
            )
        if key.startswith("soil_moisture") and not 0 <= value <= 1:
            raise ProviderError(
                "SOIL_MOISTURE_RANGE_INVALID", "Volumetric soil moisture must be between 0 and 1"
            )
        availability = (
            Availability.available if summary.valid_fraction >= 0.999 else Availability.partial
        )
        reason = (
            None
            if availability == Availability.available
            else Reason(
                code="NATIVE_PIXEL_COVERAGE_PARTIAL",
                values={
                    "valid_fraction": summary.valid_fraction,
                    "valid_pixels": summary.valid_pixels,
                },
            )
        )
        notes = list(summary.notes)
        if str(asset["dataset_id"]).lower().startswith("soilgrids"):
            notes.append(
                "ISRIC advises SoilGrids as global/regional context, not validated farm-level soil measurements."
            )
        if asset["dataset_id"].startswith("SPL4SM") and not asset.get("quality_path"):
            notes.append(
                "SMAP nodata mask is applied; no separate retrieval/analysis quality field was supplied."
            )
        if asset["format"] == "netcdf":
            notes.append(
                "Temporal means are context only; inspect the registered period and native time sampling."
            )
        period = (
            DateRange(
                start=date.fromisoformat(str(asset["start"])[:10]),
                end=date.fromisoformat(str(asset.get("end", asset["start"]))[:10]),
            )
            if asset.get("start")
            else None
        )
        if (
            period
            and variable in {"soil_moisture", "soil_moisture_surface", "ndvi"}
            and (period.start > start or period.end < end)
        ):
            availability = Availability.partial
            reason = Reason(
                code="ASSET_TEMPORAL_COVERAGE_PARTIAL",
                values={
                    "requested_start": start.isoformat(),
                    "requested_end": end.isoformat(),
                    "source_start": period.start.isoformat(),
                    "source_end": period.end.isoformat(),
                },
            )
            notes.append(
                "Registered observations cover only part of the requested period; values were not extended to missing dates."
            )
        digest = file_hash(path)
        lineage = digest + "".join(
            file_hash(managed_path(self.cache_dir, asset[name]))
            for name in ("red_path", "nir_path", "quality_path")
            if asset.get(name)
        )
        eid = (
            "eo-"
            + hashlib.sha256(
                (asset["id"] + lineage + key + start.isoformat() + end.isoformat()).encode()
            ).hexdigest()[:20]
        )
        evidence = Evidence(
            id=eid,
            source_id=asset["dataset_id"],
            source_version=asset["version"],
            origin=Origin.modeled,
            availability=availability,
            synthetic=False,
            period=period,
            geographic_bounds=tuple(shape(geometry).bounds),
            resolution_m=summary.resolution_m,
            depth_cm=depth,
            reason=reason,
            source_url=asset.get("source_url"),
            quality_notes=notes,
            uncertainty=asset.get(
                "uncertainty",
                "Prediction interval not supplied; gridded estimate is not a field test.",
            ),
            unit_conversion=UnitConversion(
                original_unit=asset.get("original_unit", str(asset.get("unit", "unknown"))),
                canonical_unit=canonical,
                multiplier=Decimal(str(summary.scale * factor)),
                offset=Decimal(str(summary.offset * factor)),
            ),
        )
        if variable == "elevation" or (variable == "ndvi" and asset.get("red_path")):
            evidence.unit_conversion = None
            evidence.quality_notes.append(
                "Output is a derived nonlinear metric; its units are not a linear conversion of the input band values."
            )
        metric = Metric(
            value=_decimal(value),
            unit=canonical,
            availability=availability,
            origin=Origin.modeled,
            evidence_ids=[eid],
            depth_cm=depth,
            reason=reason,
        )
        reference = AssetReference(
            id=asset["id"],
            dataset_id=asset["dataset_id"],
            path=str(path),
            sha256=digest,
            format=asset["format"],
            period=period,
            variables=[key],
        )
        return key, metric, evidence, reference

    def health(self) -> dict:
        """Report configuration and dependency presence without exposing credentials."""
        from importlib.util import find_spec

        try:
            assets = load_assets(self.cache_dir)
            manifest_status = "valid"
        except (ValueError, OSError):
            assets = []
            manifest_status = "invalid"
        return {
            "version": PROFILE_VERSION,
            "network_enabled": self.network_enabled,
            "remote_acquisition_enabled": self.remote_assets_enabled,
            "earthdata_credentials_configured": bool(
                (os.getenv("EARTHDATA_USERNAME") and os.getenv("EARTHDATA_PASSWORD"))
                or (Path.home() / ".netrc").is_file()
            ),
            "soilgrids_wcs_enabled": self.soilgrids_enabled,
            "registered_assets": len(assets),
            "manifest_status": manifest_status,
            "providers": [
                {
                    "id": "NASA_POWER_DAILY",
                    "access": "public_api",
                    "state": "enabled" if self.network_enabled else "cache_only",
                    "variables": {
                        "T2M": "C",
                        "T2M_MIN": "C",
                        "T2M_MAX": "C",
                        "PRECTOTCORR": "mm/day",
                    },
                    "source_url": "https://power.larc.nasa.gov/docs/services/api/temporal/daily/",
                },
                {
                    "id": "SoilGrids",
                    "access": "WCS_or_registered_subset",
                    "state": "enabled"
                    if self.network_enabled and self.soilgrids_enabled
                    else "registered_or_cache_only",
                },
                *[
                    {
                        "id": asset["dataset_id"],
                        "version": asset["version"],
                        "access": "registered_asset",
                        "variable": asset.get("variable"),
                        "unit": asset.get("unit"),
                        "state": "configured",
                    }
                    for asset in assets
                ],
            ],
            "dependencies": {
                name: find_spec(name) is not None
                for name in ("rasterio", "xarray", "pyarrow", "earthaccess")
            },
            "earthdata_environment_configured": bool(
                os.getenv("EARTHDATA_USERNAME") and os.getenv("EARTHDATA_PASSWORD")
            ),
        }
