"""Explicitly registered, immutable local scientific assets.

Inputs are copied into the managed cache by content identity. A manifest cannot
escape this directory and is data, never executable provider code.
"""

import hashlib
import json
import os
import shutil
from pathlib import Path
from threading import RLock

_LOCK = RLock()
FORMATS = {"power_json", "giovanni_csv", "geotiff", "netcdf", "parquet"}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{id(payload)}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def managed_path(cache_dir: Path, value: str) -> Path:
    root = cache_dir.resolve()
    path = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Asset paths must remain inside the managed cache directory")
    return path


def load_assets(cache_dir: Path) -> list[dict]:
    manifest = Path(cache_dir) / "assets.json"
    if not manifest.exists():
        return []
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assets = data.get("assets", []) if isinstance(data, dict) else data
    if not isinstance(assets, list):
        raise ValueError("Asset manifest requires an assets list")
    ids = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("Asset manifest entries must be objects")
        for key in ("id", "dataset_id", "version", "format", "path"):
            if not asset.get(key):
                raise ValueError(f"Asset manifest entry requires {key}")
        if asset["id"] in ids:
            raise ValueError("Asset IDs must be unique")
        ids.add(asset["id"])
        if asset["format"] not in FORMATS:
            raise ValueError("Unsupported scientific asset format")
        for key in ("path", "red_path", "nir_path", "quality_path"):
            if asset.get(key):
                path = managed_path(Path(cache_dir), asset[key])
                if not path.is_file():
                    raise ValueError(f"Registered asset is missing: {asset['id']} ({key})")
                if asset.get(key + "_sha256") and file_hash(path) != asset[key + "_sha256"]:
                    raise ValueError(
                        f"Registered companion checksum changed: {asset['id']} ({key})"
                    )
        if (
            asset.get("sha256")
            and file_hash(managed_path(Path(cache_dir), asset["path"])) != asset["sha256"]
        ):
            raise ValueError(f"Registered asset checksum changed: {asset['id']}")
    return assets


def register_asset(
    cache_dir: Path,
    path: Path,
    dataset_id: str,
    format: str,
    variable: str | None = None,
    unit: str | None = None,
    version: str = "unspecified",
    **metadata,
) -> dict:
    """Copy a selected local file, then upsert its source/version manifest entry.

    Optional red_path/nir_path/quality_path files are copied too for HLS NDVI.
    An explicit bounds/point, depth, period, source_url and scale may be supplied.
    """
    if format not in FORMATS:
        raise ValueError(f"format must be one of {sorted(FORMATS)}")
    if not dataset_id.strip() or not version.strip():
        raise ValueError("Dataset ID and version must be nonempty")
    root = Path(cache_dir).resolve()
    (root / "assets").mkdir(parents=True, exist_ok=True)

    def copy_file(source: Path) -> tuple[str, str]:
        source = source.resolve()
        if not source.is_file():
            raise ValueError("A scientific input file is required")
        digest = file_hash(source)
        destination = root / "assets" / (digest + source.suffix.lower())
        if not destination.exists():
            shutil.copyfile(source, destination)
        return str(destination.relative_to(root)), digest

    location, digest = copy_file(Path(path))
    extras = dict(metadata)
    for key in ("red_path", "nir_path", "quality_path"):
        if extras.get(key):
            extras[key], extras[key + "_sha256"] = copy_file(Path(extras[key]))
    identity = hashlib.sha256(
        json.dumps(
            {
                "sha256": digest,
                "dataset_id": dataset_id,
                "version": version,
                "variable": variable,
                "metadata": extras,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()[:24]
    record = {
        **extras,
        "id": "asset-" + identity,
        "dataset_id": dataset_id,
        "version": version,
        "format": format,
        "path": location,
        "sha256": digest,
        "variable": variable,
        "unit": unit,
    }
    with _LOCK:
        existing = load_assets(root)
        existing = [item for item in existing if item["id"] != record["id"]]
        existing.append(record)
        atomic_json(root / "assets.json", {"version": "1.0", "assets": existing})
    return record
