"""Packaged, versioned primary-source rules; no run-time network requests."""

from importlib.resources import files

import yaml
from shapely.geometry import box, shape

from agrix.models import Availability, Catalog, EnvironmentalParameters, Evidence, Origin, Plot


def load_agronomic_catalog() -> Catalog:
    raw = files("agrix").joinpath("fixtures/morocco_catalog.yaml").read_text(encoding="utf-8")
    catalog = Catalog.model_validate(yaml.safe_load(raw))
    rules = yaml.safe_load(
        files("agrix").joinpath("fixtures/environmental_rules.yaml").read_text(encoding="utf-8")
    )
    catalog.version = catalog.fixture_version = rules["version"]
    for crop in catalog.crops:
        if crop.fallow:
            continue
        values = dict(rules["crops"][crop.id])
        ecocrop_id = values.pop("ecocrop_id")
        ref = {
            "id": f"fao-ecocrop-{ecocrop_id}",
            "source_id": "fao-ecocrop",
            "source_version": f"species record {ecocrop_id}; reviewed {rules['reviewed_on']}",
            "url": f"https://ecocrop.apps.fao.org/ecocrop/srv/en/dataSheet?id={ecocrop_id}",
            "origin": "observed",
            "topics": ["temperature", "soil", "duration"],
            "quality_notes": [
                "Broad species ecology; not cultivar-specific Moroccan calibration.",
                "Annual rainfall ranges are not applied to a crop-season rainfall total.",
            ],
        }
        references = [ref, *rules["references"]]
        crop.references.extend(references)
        values["source_ids"] = [r["id"] for r in references]
        crop.environmental_parameters = EnvironmentalParameters.model_validate(values)
        crop.zone_ids.append("morocco_screening")
    return catalog


def reference_evidence(catalog: Catalog) -> list[Evidence]:
    """References are deduplicated by versioned ID, including explicit assumptions."""
    references = {r["id"]: r for crop in catalog.crops for r in crop.references}
    return [
        Evidence(
            id=ref["id"],
            source_id=ref["source_id"],
            source_version=ref["source_version"],
            origin=Origin(ref.get("origin", "observed")),
            synthetic=False,
            source_url=ref.get("url"),
            quality_notes=ref.get("quality_notes", []),
            uncertainty=ref.get("uncertainty"),
        )
        for ref in references.values()
    ]


def resolve_rule_coverage(plot: Plot, catalog: Catalog) -> tuple[str | None, Availability]:
    """Require the whole polygon within a documented lookup extent.

    These conservative pilot extents identify named source regions, not official
    agroecological boundaries. A match therefore remains partial/confirmation-needed.
    A crossing polygon is never assigned by centroid or nearest region.
    """
    polygon = shape(plot.geometry.model_dump())
    regions = {r["zone_id"]: r for c in catalog.crops for r in c.geographic_coverage}
    matches = []
    intersects = False
    for zone_id, region in regions.items():
        extent = box(*region["bounds"])
        intersects |= extent.intersects(polygon)
        systems = region.get("production_systems", [])
        if systems and plot.constraints.production_system not in systems:
            continue
        if extent.covers(polygon):
            matches.append((extent.area, zone_id, region))
    if matches:
        _, zone_id, region = min(matches, key=lambda match: (match[0], match[1]))
        status = Availability.partial if region.get("approximate", True) else Availability.available
        return zone_id, status
    return None, Availability.partial if intersects else Availability.unsupported
