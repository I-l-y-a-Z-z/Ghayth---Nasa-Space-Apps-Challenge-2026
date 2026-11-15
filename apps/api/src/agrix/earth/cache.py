"""Content-addressed processed daily assets, with Parquet and metadata sidecars."""

import hashlib
import json
from datetime import date
from pathlib import Path

from agrix.earth.manifest import atomic_json, file_hash


def write_weather(cache_dir: Path, rows: list[dict], identity: dict) -> tuple[Path, str, str]:
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    directory = Path(cache_dir) / "processed"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        path = directory / f"weather-{key}.json"
        atomic_json(path, {"identity": identity, "rows": rows})
        return path, file_hash(path), "json"
    path = directory / f"weather-{key}.parquet"
    schema = pa.schema(
        [
            pa.field("date", pa.date32()),
            pa.field("rain_mm", pa.float64()),
            pa.field("tmin_c", pa.float64()),
            pa.field("tmax_c", pa.float64()),
            pa.field("tmean_c", pa.float64()),
            pa.field("humidity_pct", pa.float64()),
            pa.field("wind_m_s", pa.float64()),
            pa.field("solar_mj_m2", pa.float64()),
            pa.field("root_zone_moisture", pa.float64()),
            pa.field("surface_moisture", pa.float64()),
            pa.field("evidence_ids", pa.list_(pa.string())),
        ],
        metadata={b"agrix_identity": json.dumps(identity, sort_keys=True, default=str).encode()},
    )
    data = [{**row, "date": date.fromisoformat(str(row["date"])[:10])} for row in rows]
    table = pa.Table.from_pylist(data, schema=schema)
    temporary = path.with_suffix(".tmp")
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(path)
    atomic_json(path.with_suffix(".metadata.json"), identity)
    return path, file_hash(path), "parquet"
