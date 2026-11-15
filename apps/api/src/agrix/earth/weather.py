"""NASA POWER and Giovanni import adapters, preserving UTC and product identity."""

import csv
import hashlib
import io
import json
import math
from datetime import date
from pathlib import Path

import httpx

from agrix.earth.manifest import atomic_json
from agrix.earth.records import ProviderError, WeatherSeries, dates

POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
POWER_PARAMETERS = "T2M,T2M_MIN,T2M_MAX,PRECTOTCORR,RH2M,WS2M,ALLSKY_SFC_SW_DWN"
WEATHER_COLUMNS = (
    "rain_mm",
    "tmin_c",
    "tmax_c",
    "tmean_c",
    "humidity_pct",
    "wind_m_s",
    "solar_mj_m2",
    "root_zone_moisture",
    "surface_moisture",
)
METEOROLOGY_COLUMNS = WEATHER_COLUMNS[1:7]


def numeric(value, fill=None):
    if value is None or value == fill:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result != fill else None


def _convert(value, unit: str, target: str):
    if value is None:
        return None
    normalized = unit.lower().replace(" ", "").replace("°", "")
    if target == "C":
        if normalized in {"c", "degc", "celsius"}:
            return value
        if normalized in {"k", "kelvin"}:
            return value - 273.15
    elif target == "mm/day":
        if normalized in {"mm/day", "mm/d", "mmday-1", "mm"}:
            return value
        if normalized in {"m/day", "m/d", "m"}:
            return value * 1000
    elif target == "%" and normalized in {"%", "percent"}:
        return value
    elif target == "m/s" and normalized in {"m/s", "ms-1"}:
        return value
    elif target == "MJ/m2/day":
        if normalized in {"mj/m^2/day", "mj/m2/day", "mj/m²/day"}:
            return value
        if normalized in {"kw-hr/m^2/day", "kwh/m^2/day", "kwh/m2/day"}:
            return value * 3.6
    raise ProviderError("UNSUPPORTED_PROVIDER_UNIT", f"Unsupported {target} source unit: {unit}")


def load_power_json(path: Path | dict) -> WeatherSeries:
    payload = json.loads(path.read_text(encoding="utf-8")) if isinstance(path, Path) else path
    header = payload.get("header", {})
    if header.get("time_standard", "").upper() != "UTC":
        raise ProviderError(
            "TIME_STANDARD_MISMATCH", "POWER input must explicitly use UTC daily values"
        )
    try:
        longitude, latitude, *_ = payload["geometry"]["coordinates"]
        raw = payload["properties"]["parameter"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderError(
            "INVALID_POWER_FILE", "POWER response lacks point geometry or daily parameters"
        ) from exc
    mapping = {
        "T2M": "tmean_c",
        "T2M_MIN": "tmin_c",
        "T2M_MAX": "tmax_c",
        "PRECTOTCORR": "rain_mm",
        "RH2M": "humidity_pct",
        "WS2M": "wind_m_s",
        "ALLSKY_SFC_SW_DWN": "solar_mj_m2",
    }
    metadata = payload.get("parameters", {})
    if not isinstance(raw, dict) or any(not isinstance(values, dict) for values in raw.values()):
        raise ProviderError("INVALID_POWER_FILE", "POWER parameters must contain daily mappings")
    keys = sorted(
        {key for values in raw.values() for key in values if isinstance(key, str) and len(key) == 8}
    )
    rows = []
    for key in keys:
        try:
            day = date(int(key[:4]), int(key[4:6]), int(key[6:8]))
        except ValueError:
            continue
        row = {"date": day.isoformat()}
        for source, target in mapping.items():
            value = numeric(raw.get(source, {}).get(key), header.get("fill_value", -999))
            if value is not None and source in {"T2M", "T2M_MIN", "T2M_MAX", "PRECTOTCORR"}:
                target_unit = "mm/day" if target == "rain_mm" else "C"
                value = _convert(value, metadata.get(source, {}).get("units", ""), target_unit)
            if target == "rain_mm" and value is not None and value < 0:
                value = None
            if value is not None and target in {"humidity_pct", "wind_m_s", "solar_mj_m2"}:
                unit = {"humidity_pct": "%", "wind_m_s": "m/s", "solar_mj_m2": "MJ/m2/day"}[target]
                value = _convert(value, metadata.get(source, {}).get("units", ""), unit)
                if value < 0 or (target == "humidity_pct" and value > 100):
                    value = None
            row[target] = value
        rows.append(row)
    if not rows:
        raise ProviderError("EMPTY_POWER_FILE", "POWER response contains no daily records")
    return WeatherSeries(
        dataset_id="NASA_POWER_DAILY",
        version=str(header.get("api", {}).get("version", "unspecified")),
        source_url="https://power.larc.nasa.gov/docs/services/api/temporal/daily/",
        rows=rows,
        bounds=(
            round(float(longitude) / 0.625) * 0.625 - 0.3125,
            round(float(latitude) / 0.5) * 0.5 - 0.25,
            round(float(longitude) / 0.625) * 0.625 + 0.3125,
            round(float(latitude) / 0.5) * 0.5 + 0.25,
        ),
        units={k: str(v.get("units", "")) for k, v in metadata.items()},
        notes=[
            "Regional reanalysis, not a plot weather station.",
            "Approximate native meteorological footprint: 0.5° latitude × 0.625° longitude.",
            "PRECTOTCORR is the explicitly labeled coarse rainfall fallback, not IMERG Final.",
        ],
        path=path if isinstance(path, Path) else None,
    )


def load_giovanni_csv(path: Path) -> WeatherSeries:
    text = path.read_text(encoding="utf-8-sig")
    all_rows = list(csv.reader(io.StringIO(text)))
    table_index = next(
        (i for i, row in enumerate(all_rows) if row and row[0].strip().lower() == "time"), None
    )
    if table_index is None:
        raise ProviderError("INVALID_GIOVANNI_FILE", "Giovanni CSV requires a daily time table")
    metadata = {
        row[0].strip().rstrip(":"): row[1].strip()
        for row in all_rows[:table_index]
        if len(row) >= 2
    }
    identity = " ".join([path.name, metadata.get("Title", ""), *all_rows[table_index]])
    if "GPM_3IMERGDF" in identity or "Final Run" in identity:
        short_name = "GPM_3IMERGDF"
    elif "GPM_3IMERGDL" in identity or "Late Run" in identity:
        short_name = "GPM_3IMERGDL"
    elif "GPM_3IMERGDE" in identity or "Early Run" in identity:
        short_name = "GPM_3IMERGDE"
    else:
        raise ProviderError(
            "UNIDENTIFIED_PRECIPITATION_PRODUCT", "CSV must identify IMERG Final, Late or Early"
        )
    try:
        bounds = tuple(float(v) for v in metadata["User Bounding Box"].split(","))
        if len(bounds) != 4:
            raise ValueError
    except (ValueError, KeyError) as exc:
        raise ProviderError(
            "MISSING_SPATIAL_LINEAGE", "Area-average CSV requires its requested bounding box"
        ) from exc
    fill = next((numeric(v) for k, v in metadata.items() if k.startswith("Fill Value")), -9999.9)
    rows = []
    for raw in all_rows[table_index + 1 :]:
        if len(raw) < 2:
            continue
        try:
            day = date.fromisoformat(raw[0].strip()[:10])
        except ValueError:
            continue
        rain = numeric(raw[1].strip(), fill)
        rows.append(
            {"date": day.isoformat(), "rain_mm": rain if rain is None or rain >= 0 else None}
        )
    if not rows or len({r["date"] for r in rows}) != len(rows):
        raise ProviderError("INVALID_DAILY_SERIES", "CSV must contain unique daily records")
    return WeatherSeries(
        dataset_id=short_name,
        version="07" if "_07" in identity else "unspecified",
        source_url=f"https://doi.org/10.5067/GPM/{short_name.removeprefix('GPM_3')}/DAY/07",
        rows=rows,
        bounds=bounds,
        units={"rain_mm": "mm/day"},
        path=path,
        notes=[
            "Spatial average over the export box; individual plot pixels cannot be recovered.",
            "Daily mm/day attributed from the provider product documentation; CSV unit label is absent.",
            "Native retrieval counts and pixel uncertainty are absent from this export.",
        ],
    )


class PowerProvider:
    def __init__(self, cache_dir: Path, network_enabled: bool = True, client=None):
        self.cache_dir = Path(cache_dir)
        self.network_enabled = network_enabled
        self.client = client

    def fetch(self, longitude: float, latitude: float, start: date, end: date) -> WeatherSeries:
        dates(start, end)
        # Explicit native meteorological grid-center requests deduplicate nearby plots.
        lon = round(longitude / 0.625) * 0.625
        lat = round(latitude / 0.5) * 0.5
        request = {
            "parameters": POWER_PARAMETERS,
            "community": "AG",
            "longitude": lon,
            "latitude": lat,
            "start": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "format": "JSON",
            "time-standard": "UTC",
        }
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / "raw" / f"power-{key}.json"
        if path.exists():
            return load_power_json(path)
        if not self.network_enabled:
            raise ProviderError(
                "NETWORK_DISABLED", "No cached POWER data; provider network access is disabled"
            )
        try:
            if self.client is not None:
                response = self.client.get(POWER_URL, params=request, timeout=45)
            else:
                with httpx.Client(timeout=45, follow_redirects=True) as client:
                    response = client.get(POWER_URL, params=request)
            response.raise_for_status()
            payload = response.json()
            parsed = load_power_json(payload)
        except ProviderError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(
                "POWER_REQUEST_FAILED", "POWER could not return valid daily data"
            ) from exc
        atomic_json(path, payload)
        parsed.path = path
        return parsed
