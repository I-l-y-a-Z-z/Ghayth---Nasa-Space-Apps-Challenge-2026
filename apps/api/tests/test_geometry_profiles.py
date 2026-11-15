from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pyproj import Geod
from shapely.geometry import Polygon as ShapelyPolygon

from agrix.errors import DomainError
from agrix.geometry import normalize_geometry
from agrix.models import Availability, FarmConstraints, Metric, Origin, Plot, Polygon
from agrix.profiles import FIXTURE_VERSION, get_profile


def rectangle(west=-7.61, south=32.99, east=-7.60, north=33.0):
    return [[(west, south), (east, south), (east, north), (west, north), (west, south)]]


def make_plot(coordinates=None, revision=1):
    geometry, area = normalize_geometry(Polygon(coordinates=coordinates or rectangle()))
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return Plot(
        id="plot-test",
        project_id="project-test",
        name="Fixture plot",
        revision=revision,
        geometry=geometry,
        area_ha=area,
        constraints=FarmConstraints(),
        created_at=now,
        updated_at=now,
    )


def test_area_is_geodesic_and_normalized_deterministically():
    coordinates = rectangle()
    normalized, area = normalize_geometry(Polygon(coordinates=coordinates))
    reversed_geometry, reversed_area = normalize_geometry(
        Polygon(coordinates=[list(reversed(coordinates[0]))])
    )
    assert normalized == reversed_geometry
    assert area == reversed_area
    assert ShapelyPolygon(normalized.coordinates[0]).exterior.is_ccw
    expected, _ = Geod(ellps="WGS84").polygon_area_perimeter(
        [point[0] for point in coordinates[0]], [point[1] for point in coordinates[0]]
    )
    assert abs(area - Decimal(str(expected)) / 10000) <= Decimal("0.000001")


def test_holes_subtract_area_and_have_clockwise_orientation():
    outer = rectangle(-7.62, 32.98, -7.60, 33.0)[0]
    hole = rectangle(-7.615, 32.985, -7.605, 32.995)[0]
    normalized, area = normalize_geometry(Polygon(coordinates=[outer, hole]))
    _, outer_area = normalize_geometry(Polygon(coordinates=[outer]))
    _, hole_area = normalize_geometry(Polygon(coordinates=[hole]))
    assert abs(area - (outer_area - hole_area)) <= Decimal("0.000001")
    assert not ShapelyPolygon(normalized.coordinates[1]).exterior.is_ccw


@pytest.mark.parametrize(
    "coordinates",
    [
        [],
        [[(-7.6, 33.0), (-7.5, 33.0), (-7.5, 33.1)]],
        [[(-7.6, 33.0)] * 4],
        [[(-7.6, 33.0), (-7.5, 33.0), (-7.4, 33.0), (-7.6, 33.0)]],
        [[(-7.6, 33.0), (-7.5, 33.1), (-7.5, 33.0), (-7.6, 33.1), (-7.6, 33.0)]],
        rectangle() + rectangle(-7.0, 33.0, -6.9, 33.1),
        [[(-7.6, 33.0), (float("nan"), 33.0), (-7.5, 33.1), (-7.6, 33.0)]],
        [[(-7.6, 33.0), (-7.5, float("inf")), (-7.5, 33.1), (-7.6, 33.0)]],
        rectangle(181, 33.0, 182, 33.1),
    ],
)
def test_invalid_polygons_are_rejected(coordinates):
    with pytest.raises(DomainError) as caught:
        normalize_geometry(Polygon(coordinates=coordinates))
    assert caught.value.code == "INVALID_GEOMETRY"


def test_unsupported_geography_is_explicit():
    with pytest.raises(DomainError) as caught:
        normalize_geometry(Polygon(coordinates=rectangle(2.0, 48.0, 2.1, 48.1)))
    assert caught.value.code == "UNSUPPORTED_GEOGRAPHY"
    assert "not an authoritative" in caught.value.message


@pytest.mark.parametrize(
    "coordinates, zone_id, rule_coverage, missing_ph",
    [
        (rectangle(), "settat_demo", Availability.available, False),
        (rectangle(-5.6, 33.9, -5.59, 33.91), "meknes_demo", Availability.available, True),
        (rectangle(-6.9, 31.0, -6.89, 31.01), "ouarzazate_demo", Availability.unsupported, False),
    ],
)
def test_fixture_profiles_separate_environment_and_rules(
    coordinates, zone_id, rule_coverage, missing_ph
):
    plot = make_plot(coordinates, revision=3)
    profile = get_profile(plot)
    assert profile.zone_id == zone_id
    assert profile.plot_id == plot.id
    assert profile.plot_revision == 3
    assert profile.fixture_version == FIXTURE_VERSION
    assert profile.rule_coverage == rule_coverage
    assert (profile.metrics["soil_ph"].value is None) == missing_ph
    assert profile.metrics["soil_moisture"].depth_cm == (0, 100)
    assert all(
        item.synthetic and item.source_id.startswith("fixture-") for item in profile.evidence
    )
    assert all(item.source_version == FIXTURE_VERSION for item in profile.evidence)
    for item in profile.evidence:
        assert (item.period is None) == (item.source_id == "fixture-soil_properties")


def test_partial_profile_does_not_replace_missing_value_with_zero():
    profile = get_profile(make_plot(rectangle(-5.6, 33.9, -5.59, 33.91)))
    metric = profile.metrics["soil_ph"]
    assert metric.value is None
    assert metric.availability == Availability.missing
    assert metric.reason.code == "FIXTURE_LAYER_MISSING"
    assert profile.metrics["rainfall_mm"].availability == Availability.available


def test_whole_polygon_must_fit_fixture_coverage():
    # The center remains inside Settat, but the west edge crosses its fixture extent.
    profile = get_profile(make_plot(rectangle(-7.76, 32.99, -7.70, 33.0)))
    assert profile.zone_id is None
    assert profile.rule_coverage == Availability.unsupported
    assert all(value.value is None for value in profile.metrics.values())
    assert all(value.availability == Availability.unsupported for value in profile.metrics.values())
    assert all(value.reason for value in profile.metrics.values())


def test_outside_zones_does_not_use_nearest_profile():
    profile = get_profile(make_plot(rectangle(-9.60, 30.40, -9.59, 30.41)))
    assert profile.zone_id is None
    assert all(layer.availability == Availability.unsupported for layer in profile.layers)


def test_fixture_profile_snapshots_are_independent():
    plot = make_plot()
    first = get_profile(plot)
    first.metrics["rainfall_mm"].value = Decimal(0)
    second = get_profile(plot)
    assert second.metrics["rainfall_mm"].value == Decimal("350")
    assert first is not second


def test_legitimate_zero_is_available_not_missing():
    metric = Metric(
        value=Decimal(0), unit="mm", availability=Availability.available, origin=Origin.observed
    )
    assert metric.value == 0
    assert metric.reason is None
