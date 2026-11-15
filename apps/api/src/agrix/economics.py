"""Resolve published Moroccan economic references without asking farmers for prices."""

import json
import os
from datetime import date
from decimal import Decimal
from importlib.resources import files
from pathlib import Path

from agrix.models import Availability, DateRange, Evidence, Origin, money
from agrix.records import _applies


def published_records():
    override = os.getenv("AGRIX_ECONOMICS_DATA")
    source = (
        Path(override) if override else files("agrix").joinpath("fixtures/morocco_economics.json")
    )
    return json.loads(source.read_text())["records"]


def resolve_economics(repo, plot, profile, constraints, start_date):
    result = constraints.model_copy(deep=True)
    records = published_records()
    # Actual local farm-gate/producer observations take priority over national references.
    for row in repo.list_project("local_records", plot.project_id, limit=10000):
        if row.get("record_kind") != "market" or row.get("synthetic") is not False:
            continue
        if (
            row.get("market_level") not in {"producer", "farm_gate"}
            or not _applies(row, plot, profile)[0]
        ):
            continue
        if date.fromisoformat(row["date"]) > min(date.today(), start_date):
            continue
        records.append(
            {
                **row,
                "field": "price_mad_per_kg",
                "value": row["price_mad_per_kg"],
                "year": int(row["date"][:4]),
                "source_label": row.get("source_label") or "Local producer observation",
                "notes": ["Dated producer/farm-gate observation; future sale prices can differ."],
                "local_observation": True,
            }
        )
    aliases = {
        "settat": {"settat", "ben ahmed", "khouribga-brouj"},
        "meknes": {"meknes"},
        "saiss": {"meknes"},
        "zaer": {"rabat-sale-kenitra"},
    }
    localities = aliases.get(profile.zone_id, set())

    def preference(record):
        locality = record["locality"].casefold()
        return (
            not record.get("local_observation", False),
            locality not in localities,
            record["locality"] != "Morocco",
            -(record["year"] or 0),
            record["id"],
        )

    selected = {}
    for record in sorted(records, key=preference):
        if (
            record["year"] is not None and record["year"] > min(date.today().year, start_date.year)
        ) or Decimal(record["value"]) < 0:
            continue
        selected.setdefault((record["crop_id"], record["field"]), record)
    evidence = []
    used = []
    for (crop_id, field), record in selected.items():
        target = {
            "price_mad_per_kg": result.sale_price_mad_per_kg,
            "yield_kg_per_ha": result.yield_kg_per_ha,
            "operating_cost_mad_per_ha": result.seasonal_operating_cost_mad_per_ha,
        }[field]
        if crop_id in target and record["id"] not in result.economic_evidence_ids:
            continue
        if field == "operating_cost_mad_per_ha" and any(
            key.startswith(crop_id + ":") for key in result.operation_cost_overrides
        ):
            continue
        if crop_id not in target:
            target[crop_id] = (
                Decimal(record["value"]) if field == "yield_kg_per_ha" else money(record["value"])
            )
        notes = list(record.get("notes", []))
        if record["locality"] != "Morocco" and record["locality"].casefold() not in localities:
            notes.append(
                "Regional Moroccan reference from "
                + record["locality"]
                + "; not an observation from this plot locality."
            )
        evidence.append(
            Evidence(
                id=record["id"],
                source_id=record["source_label"],
                source_version=str(record["year"])
                if record["year"]
                else "undated historical survey",
                source_url=record.get("source_url"),
                origin=Origin.observed,
                synthetic=False,
                availability=Availability.partial,
                period=DateRange(start=date(record["year"], 1, 1), end=date(record["year"], 12, 31))
                if record["year"]
                else None,
                quality_notes=notes,
            )
        )
        used.append({**record, "notes": notes})
    if used:
        if result.setup_cost_mad is None:
            result.setup_cost_mad = Decimal(0)
        result.economic_evidence_ids = list(
            dict.fromkeys([*result.economic_evidence_ids, *[e.id for e in evidence]])
        )
        evidence.append(
            Evidence(
                id="operating-budget-scope-v1",
                source_id="crop-operating-budget-scope",
                source_version="1",
                origin=Origin.assumption,
                synthetic=False,
                quality_notes=[
                    "Operating crop plan excludes equipment purchases and land acquisition.",
                    "Historical yield/price references are budgeting assumptions, not a weather-calibrated yield forecast.",
                    "Season costs are reserved up front; exact payment schedules and buyer terms are not known.",
                ],
            )
        )
        result.economic_evidence_ids = list(
            dict.fromkeys([*result.economic_evidence_ids, "operating-budget-scope-v1"])
        )
    summary = {
        "basis": "published_morocco_references",
        "historical": True,
        "currency": "MAD",
        "references": used,
        "equipment_purchase_included": False,
        "cash_timing": "season cost reserved at sowing; buyer lag is an assumption",
    }
    return result, evidence, summary
