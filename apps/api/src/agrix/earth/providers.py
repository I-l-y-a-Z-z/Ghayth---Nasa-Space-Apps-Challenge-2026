"""Bounded optional provider access; credentials never enter client payloads."""

import hashlib
import os
from datetime import date
from pathlib import Path

import httpx
from pyproj import CRS, Transformer
from shapely.geometry import shape
from shapely.ops import transform

from agrix.earth.records import ProviderError


class SoilGridsWCS:
    """Fetch a native 250 m SoilGrids subset rather than relying on its paused REST API."""

    def __init__(self, cache_dir: Path, network_enabled: bool = True, client=None):
        self.cache_dir = Path(cache_dir)
        self.network_enabled = network_enabled
        self.client = client

    def coverage(
        self, geometry: dict, property: str = "phh2o", depth: str = "0-5cm", quantile: str = "mean"
    ) -> Path:
        if property not in {"phh2o", "sand", "silt", "clay", "soc", "bdod"}:
            raise ValueError("Unsupported SoilGrids property")
        if depth not in {"0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm"}:
            raise ValueError("Unsupported SoilGrids depth")
        if quantile not in {"mean", "Q0.05", "Q0.5", "Q0.95"}:
            raise ValueError("Unsupported SoilGrids prediction statistic")
        native = CRS.from_proj4("+proj=igh +datum=WGS84 +units=m +no_defs")
        polygon = transform(
            Transformer.from_crs("EPSG:4326", native, always_xy=True).transform, shape(geometry)
        )
        west, south, east, north = polygon.buffer(250).bounds
        if (east - west) * (north - south) / 62500 > 100000:
            raise ProviderError(
                "WCS_WINDOW_TOO_LARGE", "SoilGrids native subset exceeds processing limit"
            )
        coverage_id = f"{property}_{depth}_{quantile}"
        key = hashlib.sha256(
            f"{coverage_id}:{west:.0f},{south:.0f},{east:.0f},{north:.0f}".encode()
        ).hexdigest()
        destination = self.cache_dir / "raw" / f"soilgrids-{key}.tif"
        if destination.exists():
            self._validate_soil_coverage(destination)
            return destination
        if not self.network_enabled:
            raise ProviderError("NETWORK_DISABLED", "No cached SoilGrids subset is available")
        params = [
            ("map", f"/map/{property}.map"),
            ("SERVICE", "WCS"),
            ("VERSION", "2.0.1"),
            ("REQUEST", "GetCoverage"),
            ("COVERAGEID", coverage_id),
            ("SUBSETTINGCRS", "http://www.opengis.net/def/crs/EPSG/0/152160"),
            ("SUBSET", f"X({west},{east})"),
            ("SUBSET", f"Y({south},{north})"),
            ("FORMAT", "image/tiff"),
        ]
        try:
            if self.client is not None:
                response = self.client.get(
                    "https://maps.isric.org/mapserv", params=params, timeout=25
                )
            else:
                with httpx.Client(timeout=25, follow_redirects=True) as client:
                    response = client.get("https://maps.isric.org/mapserv", params=params)
            response.raise_for_status()
            if len(response.content) > 25 * 1024 * 1024:
                raise ProviderError(
                    "WCS_RESPONSE_TOO_LARGE", "SoilGrids response exceeds bounded subset size"
                )
            if response.content[:4] not in {b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"}:
                raise ProviderError(
                    "WCS_INVALID_RESPONSE", "SoilGrids did not return a GeoTIFF subset"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(response.content)
            temporary.replace(destination)
            self._validate_soil_coverage(destination)
            return destination
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(
                "SOILGRIDS_REQUEST_FAILED", "SoilGrids WCS subset could not be retrieved"
            ) from exc

    @staticmethod
    def _validate_soil_coverage(path):
        import numpy as np
        import rasterio

        with rasterio.open(path) as source:
            values = source.read(1, masked=True)
            if not values.count() or not np.any(values.compressed() != 0):
                raise ProviderError(
                    "SOILGRIDS_EMPTY_RESPONSE",
                    "WCS returned only empty/zero padding; no soil measurement is inferred",
                )


def discover_nasa_granules(
    collection: str, version: str, bounds: tuple, start: date, end: date, *, limit: int = 10
) -> list:
    """Public CMR discovery, with a mandatory bounded result count.

    A spatial search is NOT a spatial crop. The result is intentionally not
    automatically downloaded by the profile pipeline.
    """
    if not 1 <= limit <= 50:
        raise ValueError("NASA discovery limit must be between 1 and 50")
    try:
        import earthaccess
    except ImportError as exc:
        raise ProviderError(
            "EARTHACCESS_MISSING", "Install earthaccess for NASA CMR discovery"
        ) from exc
    return earthaccess.search_data(
        short_name=collection,
        version=version,
        bounding_box=tuple(bounds),
        temporal=(start.isoformat(), end.isoformat()),
        count=limit,
    )


def download_selected_granules(
    granules: list, directory: Path, *, allow_full_granules: bool = False
) -> list[Path]:
    """Download explicitly selected granules only after a caller opts into full files.

    Use NASA Harmony first when a provider offers spatial/variable subsetting.
    This helper never masquerades a CMR bounding-box search as cropping.
    """
    if not allow_full_granules:
        raise ProviderError(
            "EXPLICIT_GRANULE_SELECTION_REQUIRED",
            "Use Harmony subsets, or explicitly opt into selected full granules",
        )
    if not granules or len(granules) > 10:
        raise ValueError("Select one to ten granules per download request")
    environment = bool(
        os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD")
    )
    netrc = (Path.home() / ".netrc").is_file()
    if not environment and not netrc:
        raise ProviderError(
            "EARTHDATA_CREDENTIALS_MISSING", "Set Earthdata credentials in the server environment"
        )
    try:
        import earthaccess
    except ImportError as exc:
        raise ProviderError(
            "EARTHACCESS_MISSING", "Install earthaccess for protected downloads"
        ) from exc
    auth = earthaccess.login(strategy="environment" if environment else "netrc", persist=False)
    if not auth.authenticated:
        raise ProviderError("EARTHDATA_AUTHENTICATION_FAILED", "Run agrix earthdata-login locally")
    Path(directory).mkdir(parents=True, exist_ok=True)
    return [
        Path(path) for path in earthaccess.download(granules, local_path=str(directory), threads=2)
    ]
