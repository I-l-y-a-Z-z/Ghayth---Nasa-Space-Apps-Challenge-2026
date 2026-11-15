"""Polygon validation and WGS84 geodesic area calculation.

The supported envelope is a deliberately broad Morocco-oriented configuration,
not an authoritative national boundary. Fixture evidence has narrower coverage.
"""

from decimal import ROUND_HALF_UP, Decimal
from math import isfinite

from pyproj import Geod
from shapely.errors import GEOSException
from shapely.geometry import Polygon as ShapelyPolygon
from shapely.geometry.polygon import orient
from shapely.validation import explain_validity

from agrix.errors import DomainError
from agrix.models import Polygon

SUPPORTED_ENVELOPE = (-17.5, 20.5, -0.9, 36.1)
AREA_PRECISION_HA = Decimal("0.000001")
_GEOD = Geod(ellps="WGS84")


def _canonical_ring(coordinates: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Keep orientation and choose a stable first vertex for equivalent rings."""
    vertices = coordinates[:-1]
    first = min(range(len(vertices)), key=vertices.__getitem__)
    ordered = vertices[first:] + vertices[:first]
    return ordered + [ordered[0]]


def normalize_geometry(polygon: Polygon) -> tuple[Polygon, Decimal]:
    """Return a valid, consistently oriented polygon and its area in hectares.

    Rings must be explicitly closed. Longitudes/latitudes are never swapped or
    silently clipped. The returned outer ring is counterclockwise and holes
    clockwise; holes are subtracted from geodesic area. Area precision is
    0.000001 ha (0.01 m²).
    """
    if not polygon.coordinates:
        raise DomainError("INVALID_GEOMETRY", "A polygon needs an exterior ring.")

    west, south, east, north = SUPPORTED_ENVELOPE
    for ring in polygon.coordinates:
        if len(ring) < 4 or ring[0] != ring[-1]:
            raise DomainError(
                "INVALID_GEOMETRY",
                "Each ring needs at least four coordinates and must end at its first coordinate.",
            )
        if len(set(ring[:-1])) < 3:
            raise DomainError("INVALID_GEOMETRY", "Each ring needs three distinct vertices.")
        for longitude, latitude in ring:
            if not isfinite(longitude) or not isfinite(latitude):
                raise DomainError("INVALID_GEOMETRY", "Coordinates must be finite numbers.")
            if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                raise DomainError(
                    "INVALID_GEOMETRY", "Use WGS84 longitude/latitude coordinate pairs."
                )
            if not (west <= longitude <= east and south <= latitude <= north):
                raise DomainError(
                    "UNSUPPORTED_GEOGRAPHY",
                    "The polygon is outside the configured Morocco-oriented envelope "
                    "(-17.5, 20.5, -0.9, 36.1). This envelope is not an authoritative "
                    "country boundary.",
                )

    try:
        shape = ShapelyPolygon(polygon.coordinates[0], polygon.coordinates[1:])
        if shape.is_empty or not shape.is_valid or shape.area <= 0:
            raise DomainError(
                "INVALID_GEOMETRY",
                f"Draw a nondegenerate polygon without crossing rings: {explain_validity(shape)}.",
            )
        oriented = orient(shape, sign=1.0)
        square_meters, _ = _GEOD.geometry_area_perimeter(oriented)
    except GEOSException as exc:
        raise DomainError("INVALID_GEOMETRY", "The polygon could not be interpreted.") from exc

    if not isfinite(square_meters) or square_meters <= 0:
        raise DomainError("INVALID_GEOMETRY", "The polygon must have positive geodesic area.")
    hectares = (Decimal(str(square_meters)) / Decimal(10000)).quantize(
        AREA_PRECISION_HA, rounding=ROUND_HALF_UP
    )
    if hectares <= 0:
        raise DomainError(
            "AREA_TOO_SMALL", "The polygon area is below the supported precision of 0.000001 ha."
        )

    exterior = _canonical_ring(list(oriented.exterior.coords))
    holes = sorted(_canonical_ring(list(ring.coords)) for ring in oriented.interiors)
    normalized = Polygon(coordinates=[exterior, *holes])
    return normalized, hectares
