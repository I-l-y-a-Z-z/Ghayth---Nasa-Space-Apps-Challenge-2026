"""Download official FAOSTAT Morocco price/yield rows; preserve reviewed ONCA budgets.

Run with the product Python: python scripts/sync_economics.py
Set AGRIX_ECONOMICS_DATA=./data/economics.json to use the refreshed file.
"""

import csv
import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import httpx

ROOT = Path(__file__).resolve().parents[1]
ITEMS = {
    "wheat": "Wheat",
    "durum_wheat": "Wheat",
    "barley": "Barley",
    "faba": "Broad beans and horse beans, dry",
    "lentil": "Lentils, dry",
    "chickpea": "Chick peas, dry",
}


def synchronize(output):
    packaged = ROOT / "apps/api/src/agrix/fixtures/morocco_economics.json"
    snapshot = json.loads(packaged.read_text())
    records = [row for row in snapshot["records"] if row["id"].startswith("onca-")]
    sources = [
        (
            "prices",
            "Prices_E_All_Data_(Normalized).zip",
            "Producer Price (LCU/tonne)",
            "price_mad_per_kg",
            "MAD/kg",
        ),
        (
            "crops",
            "Production_Crops_Livestock_E_All_Data_(Normalized).zip",
            "Yield",
            "yield_kg_per_ha",
            "kg/ha",
        ),
    ]
    for dataset, filename, element, field, unit in sources:
        url = "https://bulks-faostat.fao.org/production/" + filename
        response = httpx.get(url, follow_redirects=True, timeout=120)
        response.raise_for_status()
        with ZipFile(io.BytesIO(response.content)) as archive:
            name = next(n for n in archive.namelist() if n.endswith(".csv") and "Normalized" in n)
            with archive.open(name) as stream:
                for row in csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig")):
                    if (
                        row.get("Area") != "Morocco"
                        or row.get("Element") != element
                        or not row.get("Value")
                    ):
                        continue
                    year = int(row["Year"])
                    if year < date.today().year - 6 or year > date.today().year:
                        continue
                    if dataset == "prices" and row.get("Months") != "Annual value":
                        continue
                    if dataset == "crops" and row.get("Unit") != "kg/ha":
                        raise ValueError("FAOSTAT yield units changed; review conversion")
                    value = Decimal(row["Value"]) / (1000 if dataset == "prices" else 1)
                    if not value.is_finite() or value < 0:
                        raise ValueError("Invalid source value")
                    for crop_id, item in ITEMS.items():
                        if row["Item"] != item:
                            continue
                        notes = [
                            "National historical reference; not a plot forecast or buyer quote."
                        ]
                        if crop_id in {"wheat", "durum_wheat"}:
                            notes.append(
                                "FAOSTAT reports wheat combined; this is an aggregate wheat budgeting reference."
                            )
                        records.append(
                            dict(
                                id=f"faostat-{dataset}-{crop_id}-{year}",
                                crop_id=crop_id,
                                field=field,
                                value=str(value),
                                unit=unit,
                                year=year,
                                locality="Morocco",
                                source_url="https://www.fao.org/faostat/en/#data/"
                                + ("PP" if dataset == "prices" else "QCL"),
                                download_url=url,
                                source_label="FAOSTAT Morocco annual producer prices"
                                if dataset == "prices"
                                else "FAOSTAT Morocco crop yields",
                                flag=row.get("Flag"),
                                notes=notes,
                            )
                        )
    if not any(r["field"] == "price_mad_per_kg" for r in records) or not any(
        r["field"] == "yield_kg_per_ha" for r in records
    ):
        raise ValueError("Morocco source coverage missing; existing snapshot retained")
    snapshot.update(
        version="morocco-economics-" + date.today().isoformat(),
        retrieved_on=date.today().isoformat(),
        records=records,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)
    print(f"Saved {len(records)} sourced references to {output}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/economics.json")
    synchronize(parser.parse_args().output)
