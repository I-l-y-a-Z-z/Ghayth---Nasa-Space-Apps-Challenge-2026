import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import numpy as np
import pytest
import rasterio
import xarray as xr
from rasterio.transform import from_origin

from agrix.demo import demo_plot
from agrix.earth import ProfilePipeline, register_asset
from agrix.earth.manifest import file_hash, load_assets
from agrix.earth.providers import download_selected_granules
from agrix.earth.rasters import sample_hls_ndvi, sample_netcdf, sample_raster
from agrix.earth.records import ProviderError
from agrix.earth.weather import PowerProvider, load_giovanni_csv, load_power_json
from agrix.geometry import normalize_geometry
from agrix.models import Plot

START, END = date(2024, 10, 1), date(2024, 10, 3)


def power_payload(*, fill=False, unit="C", standard="UTC"):
    columns = {
        "T2M": {"20241001": 10, "20241002": -999 if fill else 12, "20241003": 11},
        "T2M_MIN": {"20241001": 5, "20241002": 6, "20241003": 7},
        "T2M_MAX": {"20241001": 15, "20241002": 20, "20241003": 19},
        "PRECTOTCORR": {"20241001": 0, "20241002": 3, "20241003": 1},
    }
    return {
        "header": {"api": {"version": "test-v1"}, "fill_value": -999, "time_standard": standard},
        "geometry": {"coordinates": [-7.5, 33, 100]},
        "properties": {"parameter": columns},
        "parameters": {k: {"units": "mm/day" if k == "PRECTOTCORR" else unit} for k in columns},
    }


def plot():
    request = demo_plot()
    geometry, area = normalize_geometry(request.geometry)
    now = datetime.now(timezone.utc)
    return Plot(
        id="real-plot",
        project_id="test",
        name="Test",
        revision=1,
        geometry=geometry,
        area_ha=area,
        constraints=request.constraints,
        created_at=now,
        updated_at=now,
    )


def register_power(tmp_path, *, fill=False):
    source = tmp_path / "power.json"
    source.write_text(json.dumps(power_payload(fill=fill)))
    register_asset(tmp_path / "cache", source, "NASA_POWER_DAILY", "power_json", version="test-v1")


def geotiff(path, values, *, crs="EPSG:4326", transform=None, nodata=-9999):
    values = np.asarray(values)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype=values.dtype,
        crs=crs,
        transform=transform or from_origin(-7.7, 33.2, 0.1, 0.1),
        nodata=nodata,
    ) as dst:
        dst.write(values, 1)
    return path


def test_power_adapter_preserves_missing_and_zero_and_time_standard():
    result = load_power_json(power_payload(fill=True))
    assert result.rows[0]["rain_mm"] == 0
    assert result.rows[1]["tmean_c"] is None
    assert result.rows[1]["tmax_c"] == 20
    assert result.dataset_id == "NASA_POWER_DAILY"
    with pytest.raises(ProviderError, match="UTC"):
        load_power_json(power_payload(standard="LST"))
    with pytest.raises(ProviderError, match="Unsupported"):
        load_power_json(power_payload(unit="invented"))


def test_power_kelvin_conversion_is_not_applied_twice():
    result = load_power_json(power_payload(unit="K"))
    assert result.rows[0]["tmean_c"] == pytest.approx(-263.15)


def test_optional_meteorology_survives_native_processing_parquet_and_rain_retry(tmp_path):
    payload = power_payload()
    for name, value, unit in (
        ("RH2M", 70, "%"),
        ("WS2M", 2, "m/s"),
        ("ALLSKY_SFC_SW_DWN", 4, "kW-hr/m^2/day"),
    ):
        payload["properties"]["parameter"][name] = {
            day: value for day in payload["properties"]["parameter"]["T2M"]
        }
        payload["parameters"][name] = {"units": unit}
    source = tmp_path / "optional-weather.json"
    source.write_text(json.dumps(payload))
    register_asset(tmp_path / "cache", source, "NASA_POWER_DAILY", "power_json", version="test-v1")
    pipeline = ProfilePipeline(tmp_path / "cache", network_enabled=False)
    result = pipeline.run(plot(), START, END)
    row = result.weather[0]
    assert row.humidity_pct == 70
    assert row.wind_m_s == 2
    assert row.solar_mj_m2 == Decimal("14.4")
    import pyarrow.parquet as pq

    processed = next(
        asset for asset in result.assets if asset.dataset_id == "processed_daily_weather"
    )
    cached = pq.read_table(processed.path).to_pylist()[0]
    assert cached["solar_mj_m2"] == pytest.approx(14.4)
    retry = pipeline.run(plot(), START, END, previous_profile=result, retry_layers=["rainfall"])
    assert retry.weather[0].humidity_pct == row.humidity_pct
    assert retry.weather[0].solar_mj_m2 == row.solar_mj_m2


def test_live_power_request_is_bounded_cached_and_utc(tmp_path):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=power_payload())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        provider = PowerProvider(tmp_path, True, client)
        assert len(provider.fetch(-7.6, 33, START, END).rows) == 3
        assert len(provider.fetch(-7.61, 33.01, START, END).rows) == 3
    assert len(requests) == 1
    assert requests[0].url.params["time-standard"] == "UTC"
    assert requests[0].url.params["start"] == "20241001"
    assert requests[0].url.params["end"] == "20241003"
    assert float(requests[0].url.params["longitude"]) == -7.5


@pytest.mark.parametrize(
    "run,short_name",
    [("Final", "GPM_3IMERGDF"), ("Late", "GPM_3IMERGDL"), ("Early", "GPM_3IMERGDE")],
)
def test_giovanni_import_preserves_run_identity(tmp_path, run, short_name):
    path = tmp_path / "sample.csv"
    path.write_text(
        f'Title:,"Rainfall - {run} Run (daily)"\nUser Bounding Box:,"-7.7,32.9,-7.5,33.1"\n'
        f"Fill Value ({short_name}_07_precipitation):, -9999.9\n\n"
        f"time,{short_name}_07_precipitation\n2024-10-01,0\n2024-10-02,-9999.9\n"
    )
    result = load_giovanni_csv(path)
    assert result.dataset_id == short_name and result.version == "07"
    assert result.rows[0]["rain_mm"] == 0
    assert result.rows[1]["rain_mm"] is None
    assert short_name.removeprefix("GPM_3") in result.source_url
    assert any("retrieval counts" in note for note in result.notes)


def test_pipeline_uses_actual_registered_data_and_explicit_other_gaps(tmp_path):
    register_power(tmp_path)
    updates = []
    result = ProfilePipeline(tmp_path / "cache", False, progress_callback=updates.append).run(
        plot(), START, END
    )
    assert result.synthetic is False
    assert result.fixture_version == "earth-profile-1.2.0"
    assert result.metrics["rainfall_mm"].value == Decimal(4)
    assert result.metrics["temperature_c"].value == Decimal(11)
    assert result.metrics["soil_ph"].value is None
    assert result.metrics["soil_ph"].reason.code == "LOCAL_ASSET_REQUIRED"
    assert result.rule_coverage == "unsupported"
    assert len(result.weather) == 3 and result.weather[0].rain_mm == 0
    assert all(not item.synthetic for item in result.evidence)
    assert any(item.state == "running" for item in updates)
    ids = {item.id for item in result.evidence}
    assert {i for metric in result.metrics.values() for i in metric.evidence_ids} <= ids
    assert {i for row in result.weather for i in row.evidence_ids} <= ids
    assert any(asset.format == "parquet" for asset in result.assets)
    assert all(
        file_hash(__import__("pathlib").Path(asset.path)) == asset.sha256 for asset in result.assets
    )


def test_pipeline_network_failure_never_becomes_zero_weather(tmp_path):
    def fail(request):
        return httpx.Response(503, json={"error": "provider down"})

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        result = ProfilePipeline(tmp_path, True, client=client).run(plot(), START, END)
    assert result.weather == []
    assert result.metrics["rainfall_mm"].value is None
    assert result.metrics["rainfall_mm"].reason.code == "POWER_REQUEST_FAILED"
    assert next(layer for layer in result.layers if layer.id == "temperature").state == "failed"


def test_native_weighting_and_nodata_preserve_partial_coverage(tmp_path):
    path = geotiff(
        tmp_path / "soil.tif",
        np.array([[10.0, 30.0], [-9999.0, 50.0]], dtype="float32"),
        transform=from_origin(0, 2, 1, 1),
    )
    geometry = {"type": "Polygon", "coordinates": [[(0, 1), (2, 1), (2, 2), (0, 2), (0, 1)]]}
    result = sample_raster(path, geometry)
    assert result.mean == pytest.approx(20, abs=0.01)
    assert result.valid_pixels == 2 and result.valid_fraction == pytest.approx(1, abs=0.001)
    whole = {"type": "Polygon", "coordinates": [[(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]]}
    partial = sample_raster(path, whole)
    assert partial.valid_fraction == pytest.approx(0.75, abs=0.001)
    assert partial.mean == pytest.approx(30, abs=0.01)


def test_small_plot_uses_its_native_pixel_without_inventing_resolution(tmp_path):
    path = geotiff(tmp_path / "pixel.tif", np.array([[7.4]], dtype="float32"))
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [(-7.69, 33.11), (-7.689, 33.11), (-7.689, 33.111), (-7.69, 33.111), (-7.69, 33.11)]
        ],
    }
    result = sample_raster(path, geometry)
    assert result.mean == pytest.approx(7.4)
    assert result.valid_pixels == 1 and result.resolution_m > 10000
    assert any("one native pixel" in note for note in result.notes)


def test_srtm_slope_uses_metres_not_degree_spacing(tmp_path):
    path = geotiff(
        tmp_path / "dem.tif",
        np.array([[0.0, 10.0, 20.0], [0.0, 10.0, 20.0], [0.0, 10.0, 20.0]], dtype="float32"),
        crs="EPSG:32629",
        transform=from_origin(500000, 3650010, 10, 10),
    )
    from pyproj import Transformer

    convert = Transformer.from_crs("EPSG:32629", "EPSG:4326", always_xy=True)
    ring = [
        convert.transform(x, y)
        for x, y in [
            (500010, 3650000),
            (500020, 3650000),
            (500020, 3649990),
            (500010, 3649990),
            (500010, 3650000),
        ]
    ]
    result = sample_raster(
        path, {"type": "Polygon", "coordinates": [ring]}, calculation="slope", source_unit="m"
    )
    assert result.mean == pytest.approx(45, abs=0.01)


def test_hls_ndvi_masks_cloudy_native_pixels(tmp_path):
    transform = from_origin(0, 1, 1, 1)
    red = geotiff(
        tmp_path / "red.tif", np.array([[2000, 2000]], dtype="int16"), transform=transform
    )
    nir = geotiff(
        tmp_path / "nir.tif", np.array([[6000, 9000]], dtype="int16"), transform=transform
    )
    qa = geotiff(
        tmp_path / "qa.tif", np.array([[0, 2]], dtype="uint8"), transform=transform, nodata=255
    )
    geometry = {"type": "Polygon", "coordinates": [[(0, 0), (2, 0), (2, 1), (0, 1), (0, 0)]]}
    result = sample_hls_ndvi(red, nir, qa, geometry)
    assert result.mean == pytest.approx(0.5)
    assert result.valid_pixels == 1 and result.valid_fraction == pytest.approx(0.5, abs=0.001)


def test_xarray_cf_adapter_uses_local_native_cells_and_requested_time(tmp_path):
    dataset = xr.Dataset(
        {
            "ph": (
                ("time", "lat", "lon"),
                np.array([[[6.0, 6.0], [6.0, 6.0]], [[8.0, 8.0], [8.0, 8.0]]]),
            )
        },
        coords={
            "time": np.array(["2024-10-01", "2024-10-02"], dtype="datetime64[D]"),
            "lat": [1.5, 0.5],
            "lon": [0.5, 1.5],
        },
    )
    path = tmp_path / "soil.nc"
    dataset.to_netcdf(path, engine="scipy")
    geometry = {"type": "Polygon", "coordinates": [[(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]]}
    result = sample_netcdf(path, geometry, "ph", start=START, end=START)
    assert result.mean == pytest.approx(6)


def test_registered_soil_conversion_and_depth_are_preserved(tmp_path):
    register_power(tmp_path)
    source = geotiff(tmp_path / "ph.tif", np.full((4, 4), 74, dtype="int16"))
    register_asset(
        tmp_path / "cache",
        source,
        "SoilGrids",
        "geotiff",
        "soil_ph",
        "pH*10",
        "2.0",
        depth_cm=[0, 5],
    )
    result = ProfilePipeline(tmp_path / "cache", False).run(plot(), START, END)
    metric = result.metrics["soil_ph"]
    assert metric.value == pytest.approx(Decimal("7.4")) and metric.depth_cm == (0, 5)
    source = next(e for e in result.evidence if e.id == metric.evidence_ids[0])
    assert source.unit_conversion.multiplier == Decimal(".1")
    assert any("regional context" in note for note in source.quality_notes)


def test_soil_depths_remain_separate_and_do_not_overwrite_topsoil(tmp_path):
    register_power(tmp_path)
    for depth, value in (([0, 5], 74), ([30, 60], 82)):
        source = geotiff(tmp_path / f"ph-{depth[0]}.tif", np.full((4, 4), value, dtype="int16"))
        register_asset(
            tmp_path / "cache",
            source,
            "SoilGrids",
            "geotiff",
            "soil_ph",
            "pH*10",
            "2.0",
            depth_cm=depth,
        )
    result = ProfilePipeline(tmp_path / "cache", False).run(plot(), START, END)
    assert result.metrics["soil_ph"].value == Decimal("7.4")
    assert result.metrics["soil_ph_depth_30_60_cm"].value == Decimal("8.2")
    assert result.metrics["soil_ph_depth_0_5_cm"].depth_cm == (0, 5)


def test_surface_smap_is_not_presented_as_rootzone_and_partial_dates_are_explicit(tmp_path):
    register_power(tmp_path)
    source = geotiff(tmp_path / "surface.tif", np.full((4, 4), 0.2, dtype="float32"))
    register_asset(
        tmp_path / "cache",
        source,
        "SPL4SMGP",
        "geotiff",
        "soil_moisture",
        "m3/m3",
        "008",
        depth_cm=[0, 5],
        start="2024-10-01",
        end="2024-10-01",
    )
    result = ProfilePipeline(tmp_path / "cache", False).run(plot(), START, END)
    assert result.metrics["soil_moisture"].value is None
    assert result.metrics["soil_moisture_surface"].value == pytest.approx(
        Decimal(".2"), abs=Decimal(".00001")
    )
    assert result.metrics["soil_moisture_surface"].availability == "partial"
    assert result.metrics["soil_moisture_surface"].reason.code == "ASSET_TEMPORAL_COVERAGE_PARTIAL"


def test_explicit_native_quality_rules_exclude_flagged_pixels(tmp_path):
    affine = from_origin(0, 1, 1, 1)
    source = geotiff(
        tmp_path / "data.tif", np.array([[0.2, 0.8]], dtype="float32"), transform=affine
    )
    flags = geotiff(
        tmp_path / "qa.tif", np.array([[0, 4]], dtype="uint8"), transform=affine, nodata=255
    )
    geometry = {"type": "Polygon", "coordinates": [[(0, 0), (2, 0), (2, 1), (0, 1), (0, 0)]]}
    result = sample_raster(source, geometry, quality_path=flags, quality_invalid_bits=4)
    assert result.mean == pytest.approx(0.2)
    assert result.valid_fraction == pytest.approx(0.5, abs=0.001)
    with pytest.raises(ProviderError, match="explicit"):
        sample_raster(source, geometry, quality_path=flags)


def test_missing_extrema_prevent_complete_temperature_layer(tmp_path):
    payload = power_payload()
    payload["properties"]["parameter"]["T2M_MAX"]["20241002"] = -999
    source = tmp_path / "power.json"
    source.write_text(json.dumps(payload))
    register_asset(tmp_path / "cache", source, "NASA_POWER_DAILY", "power_json", version="test")
    result = ProfilePipeline(tmp_path / "cache", False).run(plot(), START, END)
    assert result.metrics["temperature_c"].value == 11
    assert next(layer for layer in result.layers if layer.id == "temperature").state == "partial"
    assert result.weather[1].tmax_c is None


def test_retry_one_layer_preserves_successful_weather_without_http(tmp_path):
    register_power(tmp_path)
    pipeline = ProfilePipeline(tmp_path / "cache", False)
    first = pipeline.run(plot(), START, END)

    class FailClient:
        def get(self, *a, **k):
            raise AssertionError("Successful weather must not be requested again")

    second = ProfilePipeline(tmp_path / "cache", True, client=FailClient()).run(
        plot(), START, END, first, ["terrain"]
    )
    assert first.weather == second.weather
    assert first.metrics["temperature_c"] == second.metrics["temperature_c"]
    assert first.assets == second.assets


@pytest.mark.parametrize("selected", ["rainfall", "temperature"])
def test_targeted_weather_retry_preserves_other_daily_variables_and_lineage(tmp_path, selected):
    import pyarrow.parquet as pq

    register_power(tmp_path)
    pipeline = ProfilePipeline(tmp_path / "cache", False)
    first = pipeline.run(plot(), START, END)
    # Replace the source manifest: the unselected provider variables disappear
    # entirely, while the selected variable now has different actual input data.
    (tmp_path / "cache" / "assets.json").write_text(json.dumps({"assets": []}))
    payload = power_payload()
    parameters = payload["properties"]["parameter"]
    for name, rows in parameters.items():
        for day in rows:
            rows[day] = (
                (6 if name == "PRECTOTCORR" else -999)
                if selected == "rainfall"
                else (
                    -999
                    if name == "PRECTOTCORR"
                    else {"T2M": 30, "T2M_MIN": 20, "T2M_MAX": 40}[name]
                )
            )
    source = tmp_path / "replacement.json"
    source.write_text(json.dumps(payload))
    register_asset(tmp_path / "cache", source, "NASA_POWER_DAILY", "power_json", version="test-v1")

    class UnexpectedClient:
        def get(self, *args, **kwargs):
            raise AssertionError("An unselected missing provider must not trigger a download")

    result = ProfilePipeline(tmp_path / "cache", True, client=UnexpectedClient()).run(
        plot(), START, END, first, [selected]
    )
    retained_key = "temperature_c" if selected == "rainfall" else "rainfall_mm"
    retained_layer = "temperature" if selected == "rainfall" else "rainfall"
    assert result.metrics[retained_key] == first.metrics[retained_key]
    assert next(layer for layer in result.layers if layer.id == retained_layer) == next(
        layer for layer in first.layers if layer.id == retained_layer
    )
    retained_ids = set(first.metrics[retained_key].evidence_ids)
    for prior, updated in zip(first.weather, result.weather, strict=True):
        assert prior.date == updated.date
        columns = ["tmin_c", "tmean_c", "tmax_c"] if selected == "rainfall" else ["rain_mm"]
        assert all(getattr(updated, name) == getattr(prior, name) for name in columns)
        assert retained_ids.intersection(prior.evidence_ids) <= set(updated.evidence_ids)
        assert updated.rain_mm == 6 if selected == "rainfall" else updated.tmean_c == 30
    assert result.metrics["rainfall_mm" if selected == "rainfall" else "temperature_c"].value == (
        18 if selected == "rainfall" else 30
    )
    ids = {item.id for item in result.evidence}
    assert {eid for day in result.weather for eid in day.evidence_ids} <= ids
    cached = [asset for asset in result.assets if asset.dataset_id == "processed_daily_weather"][-1]
    rows = pq.read_table(cached.path).to_pylist()
    assert rows[0]["rain_mm"] == float(result.weather[0].rain_mm)
    assert rows[0]["tmean_c"] == float(result.weather[0].tmean_c)


def test_targeted_retry_rejects_a_changed_weather_period(tmp_path):
    register_power(tmp_path)
    pipeline = ProfilePipeline(tmp_path / "cache", False)
    first = pipeline.run(plot(), START, END)
    with pytest.raises(ValueError, match="weather period"):
        pipeline.run(plot(), START, date(2024, 10, 4), first, ["rainfall"])


def test_manifest_is_content_addressed_and_rejects_changed_or_external_paths(tmp_path):
    register_power(tmp_path)
    assets = load_assets(tmp_path / "cache")
    managed = tmp_path / "cache" / assets[0]["path"]
    managed.write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        load_assets(tmp_path / "cache")
    manifest = tmp_path / "cache" / "assets.json"
    manifest.write_text(json.dumps({"assets": [{**assets[0], "path": "../outside.json"}]}))
    with pytest.raises(ValueError, match="inside"):
        load_assets(tmp_path / "cache")


def test_protected_nasa_downloads_require_explicit_selection_and_credentials(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(ProviderError, match="explicitly"):
        download_selected_granules([object()], tmp_path)
    monkeypatch.delenv("EARTHDATA_USERNAME", raising=False)
    monkeypatch.delenv("EARTHDATA_PASSWORD", raising=False)
    with pytest.raises(ProviderError, match="credentials"):
        download_selected_granules([object()], tmp_path, allow_full_granules=True)
