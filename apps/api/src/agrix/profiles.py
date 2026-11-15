"""Pure synthetic profiles selected by full-polygon fixture coverage.

No provider files, source registry, credentials, or network are used at runtime.
These regions are test fixtures and do not establish Moroccan agronomic rules.
"""

from datetime import date
from decimal import Decimal

from shapely.geometry import Polygon as ShapelyPolygon
from shapely.geometry import box

from agrix.models import (
    Availability,
    DateRange,
    Evidence,
    LayerCoverage,
    Metric,
    Origin,
    Plot,
    PlotProfile,
    Reason,
)

FIXTURE_VERSION = "synthetic-1.0.0"

# Bounds are west/south/east/north and match only polygons fully covered by them.
FIXTURE_ZONES = {
    "settat_demo": {
        "bounds": (-7.75, 32.85, -7.45, 33.15),
        "values": ("350", "18.5", "7.4", "0.23"),
        "rule_coverage": Availability.available,
    },
    "meknes_demo": {
        "bounds": (-5.8, 33.75, -5.35, 34.15),
        "values": ("560", "16.8", None, "0.31"),
        "rule_coverage": Availability.available,
    },
    "ouarzazate_demo": {
        "bounds": (-7.1, 30.8, -6.5, 31.3),
        "values": ("110", "20.1", "8.1", "0.12"),
        "rule_coverage": Availability.unsupported,
    },
}

_METRICS = (
    ("rainfall_mm", "mm", Origin.observed, 10000.0, None, "rainfall"),
    ("temperature_c", "°C", Origin.modeled, 55000.0, None, "temperature"),
    ("soil_ph", "pH", Origin.modeled, 250.0, (0, 30), "soil_properties"),
    ("soil_moisture", "m3/m3", Origin.modeled, 9000.0, (0, 100), "soil_moisture"),
)


def get_profile(plot: Plot) -> PlotProfile:
    """Create an independent fixture snapshot without mutating the plot.

    A plot extending outside a fixture box receives unsupported values rather
    than values from its centroid or nearest region. Environmental evidence and
    applicable crop-rule coverage are separate decisions.
    """
    shape = ShapelyPolygon(plot.geometry.coordinates[0], plot.geometry.coordinates[1:])
    zone_id = None
    zone = None
    for candidate_id, candidate in FIXTURE_ZONES.items():
        if box(*candidate["bounds"]).covers(shape):
            zone_id, zone = candidate_id, candidate
            break

    bounds = zone["bounds"] if zone else tuple(shape.bounds)
    values = zone["values"] if zone else (None,) * len(_METRICS)
    rule_coverage = zone["rule_coverage"] if zone else Availability.unsupported
    evidence = []
    metrics = {}
    layers = []
    for (name, unit, origin, resolution, depth, layer_id), value in zip(
        _METRICS, values, strict=True
    ):
        availability = (
            Availability.available
            if value is not None
            else Availability.missing
            if zone
            else Availability.unsupported
        )
        reason = None
        if value is None:
            reason = Reason(
                code="FIXTURE_LAYER_MISSING" if zone else "FIXTURE_COVERAGE_UNSUPPORTED",
                values={"metric": name, "zone_id": zone_id},
            )
        evidence_id = f"fixture-{zone_id or 'unsupported'}-{name}-v1"
        period = (
            None if name == "soil_ph" else DateRange(start=date(2024, 10, 1), end=date(2025, 9, 30))
        )
        evidence.append(
            Evidence(
                id=evidence_id,
                source_id=f"fixture-{layer_id}",
                source_version=FIXTURE_VERSION,
                origin=origin,
                availability=availability,
                period=period,
                geographic_bounds=bounds,
                resolution_m=resolution,
                depth_cm=depth,
                reason=reason,
            )
        )
        metrics[name] = Metric(
            value=Decimal(value) if value is not None else None,
            unit=unit,
            availability=availability,
            origin=origin,
            evidence_ids=[evidence_id],
            reason=reason,
            depth_cm=depth,
        )
        layers.append(
            LayerCoverage(
                id=layer_id,
                availability=availability,
                evidence_ids=[evidence_id],
                reason=reason,
            )
        )

    return PlotProfile(
        plot_id=plot.id,
        plot_revision=plot.revision,
        fixture_version=FIXTURE_VERSION,
        zone_id=zone_id,
        rule_coverage=rule_coverage,
        metrics=metrics,
        layers=layers,
        evidence=evidence,
    )
