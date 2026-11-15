"""Invented plot inputs used by seed, smoke and the documented examples."""

from decimal import Decimal

from agrix.models import FarmConstraints, PlotCreate, Polygon


def demo_plot(zone: str = "settat") -> PlotCreate:
    longitude, latitude = {
        "settat": (-7.601, 33.0),
        "meknes": (-5.55, 33.9),
        "ouarzazate": (-6.91, 30.92),
    }[zone]
    return PlotCreate(
        analysis_mode="demo",
        name=f"Synthetic {zone} demonstration plot",
        geometry=Polygon(
            coordinates=[
                [
                    (longitude, latitude),
                    (round(longitude + 0.001, 6), latitude),
                    (round(longitude + 0.001, 6), round(latitude + 0.001, 6)),
                    (longitude, round(latitude + 0.001, 6)),
                    (longitude, latitude),
                ]
            ]
        ),
        constraints=FarmConstraints(
            budget_mad=Decimal("1000000.00"),
            machinery_access="available",
            labor_person_days_available=Decimal("1000"),
            previous_crops=[],
        ),
    )
