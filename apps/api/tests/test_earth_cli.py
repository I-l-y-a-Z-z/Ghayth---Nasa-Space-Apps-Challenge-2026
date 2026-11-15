"""Exercise the user's file import entrypoints against actual managed files/DB."""

import json
import sys

import pytest
from sqlalchemy.orm import Session

from agrix.cli import main, prepare_pilot
from agrix.config import Settings
from agrix.db import Repository, create_schema, make_engine


def invoke(monkeypatch, capsys, *arguments):
    monkeypatch.setattr(sys, "argv", ["agrix", *arguments])
    main()
    return json.loads(capsys.readouterr().out)


def test_cli_registers_actual_utc_power_and_reports_health(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "cache"
    monkeypatch.setenv("AGRIX_CACHE_DIRECTORY", str(cache))
    source = tmp_path / "power.json"
    source.write_text(
        json.dumps(
            {
                "header": {
                    "time_standard": "UTC",
                    "fill_value": -999,
                    "api": {"version": "checked-v1"},
                },
                "geometry": {"coordinates": [-7.5, 33, 0]},
                "properties": {"parameter": {"T2M": {"20241001": 20}}},
                "parameters": {"T2M": {"units": "C"}},
            }
        )
    )
    registered = invoke(
        monkeypatch,
        capsys,
        "import-asset",
        str(source),
        "--dataset-id",
        "NASA_POWER_DAILY",
        "--format",
        "power_json",
    )
    assert registered["version"] == "checked-v1"
    assert (cache / registered["path"]).read_bytes() == source.read_bytes()
    health = invoke(monkeypatch, capsys, "provider-health")
    assert health["registered_assets"] == 1 and health["manifest_status"] == "valid"
    assert any(provider["id"] == "NASA_POWER_DAILY" for provider in health["providers"])
    assert "EARTHDATA_PASSWORD" not in json.dumps(health)


def test_cli_rejects_mislabeled_power_time_standard_before_registering(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("AGRIX_CACHE_DIRECTORY", str(tmp_path / "cache"))
    source = tmp_path / "power.json"
    source.write_text(json.dumps({"header": {"time_standard": "LST"}}))
    with pytest.raises(SystemExit) as exc:
        invoke(
            monkeypatch,
            capsys,
            "import-asset",
            str(source),
            "--dataset-id",
            "NASA_POWER_DAILY",
            "--format",
            "power_json",
        )
    assert exc.value.code == 2
    assert not (tmp_path / "cache" / "assets.json").exists()
    assert "UTC" in capsys.readouterr().err


@pytest.mark.parametrize("suffix", ["json", "csv"])
def test_cli_imports_local_records_idempotently_with_decimal_unit_conversion(
    tmp_path, monkeypatch, capsys, suffix
):
    url = "sqlite+pysqlite:///" + str(tmp_path / "records.db")
    monkeypatch.setenv("DATABASE_URL", url)
    engine = make_engine(url)
    create_schema(engine)
    source = tmp_path / ("observations." + suffix)
    record = {
        "record_kind": "market",
        "id": "observed-wheat",
        "crop_id": "wheat",
        "date": "2024-10-01",
        "price": "350.00",
        "unit": "MAD/quintal",
        "market_level": "producer",
        "locality": "Settat",
        "source_label": "User supplied verified invoice",
    }
    if suffix == "json":
        source.write_text(json.dumps({"records": [record]}))
    else:
        import csv

        with source.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(record))
            writer.writeheader()
            writer.writerow(record)
    try:
        first = invoke(monkeypatch, capsys, "import-records", str(source), "--project-id", "farm-a")
        second = invoke(
            monkeypatch, capsys, "import-records", str(source), "--project-id", "farm-a"
        )
        assert first["created"] == 1 and second["unchanged"] == 1
        with Session(engine) as session:
            rows = Repository(session).list_project("local_records", "farm-a")
            assert len(rows) == 1 and rows[0]["price_mad_per_kg"] == "3.50"
            assert rows[0]["synthetic"] is False
            assert Repository(session).list_project("local_records", "farm-b") == []
    finally:
        engine.dispose()


def test_prepare_pilot_is_explicit_live_idempotent_and_preserves_unknown_inputs(tmp_path):
    from agrix.earth import ProfilePipeline, register_asset

    url = "sqlite+pysqlite:///" + str(tmp_path / "pilot.db")
    engine = make_engine(url)
    create_schema(engine)
    settings = Settings(database_url=url, cache_directory=tmp_path / "cache", network_enabled=False)
    source = tmp_path / "weather.json"
    source.write_text(
        json.dumps(
            {
                "header": {"time_standard": "UTC", "api": {"version": "test"}},
                "geometry": {"coordinates": [-7.5, 33, 0]},
                "properties": {
                    "parameter": {
                        name: {"20241001": value}
                        for name, value in {
                            "T2M": 20,
                            "T2M_MIN": 10,
                            "T2M_MAX": 30,
                            "PRECTOTCORR": 2,
                        }.items()
                    }
                },
                "parameters": {
                    name: {"units": "mm/day" if name == "PRECTOTCORR" else "C"}
                    for name in ["T2M", "T2M_MIN", "T2M_MAX", "PRECTOTCORR"]
                },
            }
        )
    )
    register_asset(
        settings.cache_directory, source, "NASA_POWER_DAILY", "power_json", version="test"
    )
    try:
        first = prepare_pilot(settings, wait_seconds=5, pipeline_factory=ProfilePipeline)
        second = prepare_pilot(settings, wait_seconds=5, pipeline_factory=ProfilePipeline)
        assert first["plot_id"] == second["plot_id"]
        assert first["analysis_id"] == second["analysis_id"]
        assert first["plan_id"] == second["plan_id"]
        assert (
            first["plan_status"] == "draft" and first["evaluation_status"] == "needs_confirmation"
        )
        with Session(engine) as session:
            repo = Repository(session)
            plots = repo.list_project("plots", "demo")
            assert len(plots) == 1 and plots[0]["analysis_mode"] == "live"
            constraints = plots[0]["constraints"]
            assert constraints["setup_cost_mad"] is None
            assert constraints["wage_mad_per_person_day"] is None
            assert constraints["budget_mad"] is None and constraints["previous_crops"] is None
            assert (
                constraints["yield_kg_per_ha"] == {} and constraints["sale_price_mad_per_kg"] == {}
            )
            plan = repo.get("plans", first["plan_id"])
            assert plan["synthetic"] is False
            assert (
                plan["evaluation_snapshot"]["financials"]["whole_plot"]["operating_margin"]["value"]
                is not None
            )
    finally:
        engine.dispose()
