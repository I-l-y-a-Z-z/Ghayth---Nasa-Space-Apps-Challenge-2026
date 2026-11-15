"""General NASA acquisition contracts, dated water and missing-data regressions."""

from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely.geometry import box

from agrix.earth.acquisition import DataAcquirer, acquisition_plan, five_year_period
from agrix.earth.manifest import register_asset
from agrix.earth.pipeline import ProfilePipeline
from agrix.earth.records import ProviderError
from agrix.models import DailyWeather, FarmConstraints


def test_default_five_year_period_is_general_and_has_no_future_observations():
    assert five_year_period(date(2026, 11, 15)) == (date(2021, 1, 1), date(2025, 12, 31))
    assert five_year_period(date(2027, 2, 1)) == (date(2022, 1, 1), date(2026, 12, 31))
    plan = acquisition_plan((-10, 30, -9, 31), date(2021, 1, 1), date(2025, 12, 31))
    assert plan["sources_preserved"]
    assert {x["id"] for x in plan["datasets"]} >= {
        "NASA_POWER_DAILY",
        "NASA_POWER_SMAP_L4",
        "HLS_v2",
        "NASADEM_HGT",
        "GPM_3IMERGDF",
    }


def test_smap_daily_zero_is_known_missing_day_stays_missing_and_footprint_is_scoped(tmp_path):
    geometry = box(-7.61, 33, -7.60, 33.01).__geo_interface__
    path = tmp_path / "moisture.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"date": "2025-01-01", "root_zone_moisture": 0.0, "surface_moisture": 0.2},
                {"date": "2025-01-02", "root_zone_moisture": None, "surface_moisture": None},
            ]
        ),
        path,
    )
    asset = register_asset(
        tmp_path / "cache",
        path,
        "NASA_POWER_SMAP_L4",
        "parquet",
        variable="soil_moisture_timeseries",
        version="test",
        aggregation_geometry=geometry,
    )
    pipeline = ProfilePipeline(tmp_path / "cache", network_enabled=False)
    source, _, metrics, weather = pipeline._smap_timeseries(
        asset,
        geometry,
        date(2025, 1, 1),
        date(2025, 1, 2),
        [DailyWeather(date=date(2025, 1, 1), rain_mm=4)],
    )
    assert weather[0].root_zone_moisture == 0
    assert weather[0].rain_mm == 4
    assert weather[1].root_zone_moisture is None
    assert metrics["soil_moisture"].value == 0
    assert metrics["soil_moisture"].availability.value == "partial"
    assert source.depth_cm == (0, 100)
    with pytest.raises(ProviderError, match="another polygon"):
        pipeline._smap_timeseries(
            asset, box(-5, 33, -4.9, 33.1).__geo_interface__, date(2025, 1, 1), date(2025, 1, 2), []
        )


def test_provider_failure_isolated_and_secret_error_messages_not_exported(tmp_path, monkeypatch):
    acquirer = DataAcquirer(tmp_path)

    def broken(*args):
        raise RuntimeError("secret-signed-token-that-must-not-be-returned")

    monkeypatch.setattr(acquirer, "soil", broken)
    monkeypatch.setattr(acquirer, "terrain", lambda *args: {"id": "native-NASA-test"})
    result = acquirer.acquire(
        box(-7.6, 33, -7.59, 33.01).__geo_interface__,
        date(2025, 1, 1),
        date(2025, 1, 2),
        ["soil", "terrain"],
    )
    assert result["datasets"]["terrain"]["status"] == "available"
    assert result["datasets"]["soil"]["status"] == "unavailable"
    assert "secret" not in str(result)


def test_irrigation_requires_an_identified_measurement_or_assumption():
    with pytest.raises(ValueError, match="source/assumption"):
        FarmConstraints(irrigation_mm_by_date={date(2026, 11, 1): 20})
    explicit = FarmConstraints(
        irrigation_mm_by_date={date(2026, 11, 1): 20},
        irrigation_source_label="Farmer delivery schedule; unverified",
    )
    assert explicit.irrigation_verified_on is None
    assert explicit.model_dump(mode="json")["irrigation_mm_by_date"] == {"2026-11-01": "20"}
