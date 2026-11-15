"""Internal provider records independent of HTTP and persistence."""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class WeatherSeries:
    dataset_id: str
    version: str
    source_url: str
    rows: list[dict]
    bounds: tuple[float, float, float, float]
    units: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    path: Path | None = None
    time_standard: str = "UTC"


@dataclass
class RasterSummary:
    mean: float | None
    minimum: float | None
    maximum: float | None
    valid_pixels: int
    valid_fraction: float
    covered_fraction: float
    resolution_m: float
    notes: list[str] = field(default_factory=list)
    scale: float = 1
    offset: float = 0


def dates(start: date, end: date) -> list[date]:
    from datetime import timedelta

    if end < start:
        raise ValueError("Analysis end must not precede start")
    if (end - start).days > 36525:
        raise ValueError("Analysis window is limited to 100 years")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]
