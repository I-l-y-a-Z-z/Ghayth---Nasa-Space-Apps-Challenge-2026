from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from agrix.db import Repository, create_schema, make_engine
from agrix.errors import DomainError
from agrix.models import Availability, FarmConstraints, Plot, PlotProfile, Polygon
from agrix.records import get_market_history, get_services, import_records


@pytest.fixture
def repo():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    with Session(engine) as session:
        yield Repository(session)
    engine.dispose()


def _plot(project="demo"):
    now = datetime.now(timezone.utc)
    return Plot(
        id="field",
        project_id=project,
        name="Field",
        revision=1,
        area_ha=1,
        geometry=Polygon(
            coordinates=[[(-7.60, 33), (-7.59, 33), (-7.59, 33.01), (-7.60, 33.01), (-7.60, 33)]]
        ),
        constraints=FarmConstraints(),
        created_at=now,
        updated_at=now,
    )


def _profile():
    return PlotProfile(
        plot_id="field",
        plot_revision=1,
        fixture_version="real",
        zone_id="settat",
        rule_coverage="partial",
        metrics={},
        layers=[],
        evidence=[],
        synthetic=False,
    )


def _market(**updates):
    row = {
        "record_kind": "market",
        "id": "price-1",
        "crop_id": "wheat",
        "date": "2025-11-01",
        "price": "350",
        "unit": "MAD/quintal",
        "market_level": "wholesale",
        "locality": "Settat",
        "source_label": "User-supplied market bulletin",
        "source_url": "https://example.org/bulletin",
    }
    row.update(updates)
    return row


def _service(**updates):
    row = {
        "record_kind": "service",
        "id": "service-1",
        "name": "User supplied operator",
        "service": "harvest",
        "locality": "Settat",
        "source_url": "https://example.org/public-directory",
        "verification_date": date.today().isoformat(),
    }
    row.update(updates)
    return row


def test_units_provenance_and_idempotency_are_preserved(repo):
    row = _market()
    imported = import_records(repo, [row], "demo")
    assert imported["created"] == 1
    stored = repo.get("local_records", imported["record_ids"][0])
    assert stored["original_unit"] == "MAD/quintal"
    assert stored["original_price"] == "350.00"
    assert Decimal(stored["price_mad_per_kg"]) == Decimal("3.5")
    assert Decimal(stored["price"]) == Decimal("3.5")
    assert stored["unit"] == "MAD/kg"
    assert not stored["synthetic"]
    assert import_records(repo, [row], "demo")["unchanged"] == 1
    section = get_market_history(repo, _plot(), _profile(), ["wheat"])
    assert section.availability == Availability.available
    assert section.data[0].price_mad_per_kg == Decimal("3.50")
    assert section.data[0].market_level == "wholesale"
    assert section.data[0].source_url == "https://example.org/bulletin"


def test_validation_is_atomic_and_does_not_invent_source_or_public_contacts(repo):
    with pytest.raises(DomainError, match="invalid fields"):
        import_records(
            repo, [_market(), _market(id="bad", source_label=None, source_url=None)], "demo"
        )
    assert repo.list_project("local_records", "demo") == []
    with pytest.raises(DomainError):
        import_records(repo, [_service(contact="private contact")], "demo")
    with pytest.raises(DomainError):
        import_records(repo, [_market(date=(date.today() + timedelta(days=1)).isoformat())], "demo")
    with pytest.raises(DomainError):
        import_records(repo, [_market(record_kind=[])], "demo")


def test_record_keys_and_discovery_are_project_scoped(repo):
    import_records(repo, [_market()], "project-a")
    import_records(repo, [_market(price="700")], "project-b")
    a = get_market_history(repo, _plot("project-a"), _profile(), ["wheat"])
    b = get_market_history(repo, _plot("project-b"), _profile(), ["wheat"])
    assert a.data[0].price_mad_per_kg == Decimal("3.50")
    assert b.data[0].price_mad_per_kg == Decimal("7.00")
    assert (
        get_market_history(repo, _plot("project-c"), _profile(), ["wheat"]).availability
        == Availability.missing
    )


def test_spatial_bounds_require_whole_plot_and_national_prices_are_context(repo):
    import_records(repo, [_market(geographic_bounds=[-8, 32, -7.595, 34])], "demo")
    assert (
        get_market_history(repo, _plot(), _profile(), ["wheat"]).availability
        == Availability.missing
    )
    import_records(
        repo,
        [_market(id="national", locality="Morocco", geographic_bounds=[-18, 20, 0, 37])],
        "demo",
    )
    history = get_market_history(repo, _plot(), _profile(), ["wheat"])
    assert history.availability == Availability.partial
    assert history.reason.code == "NATIONAL_MARKET_CONTEXT_ONLY"
    assert "National context" in history.data[0].locality


def test_services_require_reconfirmation_after_365_days(repo):
    import_records(
        repo,
        [
            _service(
                contact="Public directory phone",
                contact_public=True,
                verification_date=(date.today() - timedelta(days=366)).isoformat(),
            )
        ],
        "demo",
    )
    section = get_services(repo, _plot(), _profile())
    assert section.availability == Availability.partial
    assert section.reason.code == "SERVICE_RECONFIRMATION_REQUIRED"
    assert section.data[0].contact == "Public directory phone"
    assert not section.data[0].synthetic
    assert get_services(repo, _plot("other"), _profile()).availability == Availability.missing
