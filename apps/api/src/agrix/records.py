"""Validated, project-scoped local market observations and public service records."""

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, HttpUrl, ValidationError, field_validator, model_validator
from shapely.geometry import box, shape

from agrix.errors import DomainError
from agrix.models import (
    Availability,
    MarketObservation,
    Model,
    NonNegativeMoney,
    Plot,
    PlotProfile,
    Reason,
    Section,
    Supplier,
)


class _GeographicRecord(Model):
    id: str = Field(min_length=1, max_length=128)
    locality: str = Field(min_length=1, max_length=200)
    geographic_bounds: tuple[float, float, float, float] | None = None

    @field_validator("geographic_bounds")
    @classmethod
    def valid_bounds(cls, value):
        if value is not None:
            west, south, east, north = value
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValueError("Bounds must be ordered WGS84 west/south/east/north")
        return value

    @field_validator("locality")
    @classmethod
    def nonempty_locality(cls, value):
        if not value.strip():
            raise ValueError("Locality must identify a place or explicitly say Morocco")
        return value.strip()


class LocalMarketRecord(_GeographicRecord):
    record_kind: Literal["market"] = "market"
    crop_id: str = Field(min_length=1, max_length=100)
    date: date
    price: NonNegativeMoney
    unit: Literal["MAD/kg", "MAD/quintal", "MAD/tonne"] = "MAD/kg"
    market_level: Literal["producer", "farm_gate", "wholesale", "retail", "benchmark"]
    source_url: HttpUrl | None = None
    source_label: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def source_required(self):
        if not self.source_url and not (self.source_label and self.source_label.strip()):
            raise ValueError("A market record requires a source URL or source label")
        if self.date > date.today():
            raise ValueError("A historical observation cannot have a future date")
        return self


class LocalServiceRecord(_GeographicRecord):
    record_kind: Literal["service"] = "service"
    name: str = Field(min_length=1, max_length=200)
    service: str = Field(min_length=1, max_length=200)
    contact: str | None = Field(default=None, min_length=1, max_length=300)
    contact_public: bool = False
    verification_date: date
    source_url: HttpUrl

    @model_validator(mode="after")
    def public_source(self):
        if self.contact and not self.contact_public:
            raise ValueError("Supplied contact requires contact_public=true")
        if self.verification_date > date.today():
            raise ValueError("Verification date cannot be in the future")
        return self


def _key(project_id: str, kind: str, external_id: str) -> str:
    digest = hashlib.sha256(
        json.dumps([project_id, kind, external_id], ensure_ascii=False).encode()
    ).hexdigest()
    return "record-" + digest


def _parse(raw: dict, index: int):
    if not isinstance(raw, dict):
        raise DomainError("INVALID_LOCAL_RECORD", f"Record {index} must be an object")
    item = dict(raw)
    for optional in ("contact", "source_url", "source_label", "geographic_bounds"):
        if item.get(optional) == "":
            item[optional] = None
    if isinstance(item.get("geographic_bounds"), str):
        try:
            item["geographic_bounds"] = json.loads(item["geographic_bounds"])
        except ValueError as exc:
            raise DomainError(
                "INVALID_LOCAL_RECORD", f"Record {index} has invalid geographic_bounds"
            ) from exc
    # CSV/JSON entrypoints share these explicit aliases; record payloads are canonical.
    discriminator = (
        item.pop("record_kind", None) or item.pop("kind", None) or item.pop("type", None)
    )
    aliases = {
        "market": "market",
        "market_observation": "market",
        "service": "service",
        "supplier": "service",
    }
    kind = aliases.get(discriminator) if isinstance(discriminator, str) else None
    if kind is None:
        raise DomainError(
            "INVALID_LOCAL_RECORD", f"Record {index} requires record_kind market or service"
        )
    item["record_kind"] = kind
    try:
        model = (
            LocalMarketRecord.model_validate(item)
            if kind == "market"
            else LocalServiceRecord.model_validate(item)
        )
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(part) for part in error["loc"]) or "record" for error in exc.errors()}
        )
        raise DomainError(
            "INVALID_LOCAL_RECORD", f"Record {index} has invalid fields: {', '.join(fields)}"
        ) from exc
    return model


def import_records(repo, records: list[dict], project_id: str) -> dict:
    """Validate the whole batch before writing. Caller owns the transaction."""
    if not isinstance(records, list) or not records or len(records) > 5000:
        raise DomainError("INVALID_LOCAL_RECORDS", "Import between 1 and 5000 records")
    if not isinstance(project_id, str) or not project_id or len(project_id) > 128:
        raise DomainError("INVALID_PROJECT", "Supply a project ID between 1 and 128 characters")
    validated = [_parse(raw, index + 1) for index, raw in enumerate(records)]
    keys = [_key(project_id, record.record_kind, record.id) for record in validated]
    if len(set(keys)) != len(keys):
        raise DomainError(
            "DUPLICATE_LOCAL_RECORD", "Record IDs must be unique within each kind and batch"
        )
    summary = {
        "imported": len(validated),
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "market": 0,
        "service": 0,
        "record_ids": keys,
    }
    for record, key in zip(validated, keys, strict=True):
        data = record.model_dump(mode="json")
        data.update(id=key, source_record_id=record.id, project_id=project_id, synthetic=False)
        if isinstance(record, LocalMarketRecord):
            divisor = {
                "MAD/kg": Decimal(1),
                "MAD/quintal": Decimal(100),
                "MAD/tonne": Decimal(1000),
            }[record.unit]
            # Keep original input and a decimal canonical value, never convert it again.
            data["original_price"] = data["price"]
            data["original_unit"] = record.unit
            data["price_mad_per_kg"] = str(record.price / divisor)
            data["price"] = data["price_mad_per_kg"]
            data["unit"] = "MAD/kg"
        previous = repo.get("local_records", key)
        summary[
            "unchanged" if previous == data else "updated" if previous is not None else "created"
        ] += 1
        summary[record.record_kind] += 1
        if previous != data:
            repo.put("local_records", key, data)
    return summary


def _applies(record: dict, plot: Plot, profile: PlotProfile) -> tuple[bool, bool]:
    """Return applicable, national-context. Geographic bounds take precedence."""
    locality = str(record.get("locality", "")).casefold().strip()
    national = locality in {"morocco", "maroc", "المغرب", "national", "national - morocco"}
    if record.get("geographic_bounds") is not None:
        return box(*record["geographic_bounds"]).covers(shape(plot.geometry.model_dump())), national
    aliases = {
        "settat": {"settat", "chaouia"},
        "saiss": {"saiss", "saïs", "meknes", "meknès"},
        "meknes": {"saiss", "saïs", "meknes", "meknès"},
        "zaer": {"zaer", "zaër"},
        "abda": {"abda"},
    }
    localities = aliases.get(profile.zone_id, {profile.zone_id})
    return national or locality in localities, national


def get_market_history(
    repo, plot: Plot, profile: PlotProfile, crop_ids: list[str]
) -> Section[list[MarketObservation]]:
    observations, national_found = [], False
    for record in repo.list_project("local_records", plot.project_id, limit=10000):
        if record.get("record_kind") != "market" or record.get("synthetic") is not False:
            continue
        if record.get("crop_id") not in crop_ids:
            continue
        applies, national = _applies(record, plot, profile)
        if not applies:
            continue
        national_found |= national
        observations.append(
            MarketObservation(
                crop_id=record["crop_id"],
                date=record["date"],
                price_mad_per_kg=record["price_mad_per_kg"],
                market_level=record["market_level"],
                locality=("National context — Morocco" if national else record["locality"]),
                evidence_ids=[record["id"]],
                synthetic=False,
                source_url=record.get("source_url"),
                source_label=record.get("source_label"),
            )
        )
    observations.sort(key=lambda row: (row.date, row.crop_id, row.locality))
    if not observations:
        return Section(
            availability=Availability.missing, reason=Reason(code="LOCAL_MARKET_RECORDS_MISSING")
        )
    return Section(
        data=observations,
        availability=Availability.partial if national_found else Availability.available,
        reason=Reason(code="NATIONAL_MARKET_CONTEXT_ONLY") if national_found else None,
    )


def get_services(repo, plot: Plot, profile: PlotProfile) -> Section[list[Supplier]]:
    suppliers, stale, national_found = [], 0, False
    for record in repo.list_project("local_records", plot.project_id, limit=10000):
        if record.get("record_kind") != "service" or record.get("synthetic") is not False:
            continue
        applies, national = _applies(record, plot, profile)
        if not applies:
            continue
        national_found |= national
        verification_date = date.fromisoformat(record["verification_date"])
        stale += (date.today() - verification_date).days > 365
        suppliers.append(
            Supplier(
                id=record["id"],
                name=record["name"],
                service=record["service"],
                locality="National directory context — Morocco" if national else record["locality"],
                verification_date=verification_date,
                contact=record.get("contact") if record.get("contact_public") else None,
                geographic_bounds=record.get("geographic_bounds"),
                source_url=record["source_url"],
                synthetic=False,
            )
        )
    suppliers.sort(key=lambda row: (row.name, row.id))
    if not suppliers:
        return Section(
            availability=Availability.missing, reason=Reason(code="LOCAL_SERVICE_RECORDS_MISSING")
        )
    reason = (
        Reason(code="SERVICE_RECONFIRMATION_REQUIRED", values={"stale_records": stale})
        if stale
        else Reason(code="NATIONAL_SERVICE_CONTEXT_ONLY")
        if national_found
        else None
    )
    return Section(
        data=suppliers,
        availability=Availability.partial if reason else Availability.available,
        reason=reason,
    )
