from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from agrix.agronomy import load_agronomic_catalog
from agrix.economics import published_records, resolve_economics
from agrix.finance import build_budget
from agrix.models import CropInstance, FarmConstraints, Rotation, Scenario


class EmptyRepo:
    def list_project(self, *args, **kwargs):
        return []


def test_moroccan_sources_supply_prices_yields_and_costs_without_farmer_finances():
    constraints = FarmConstraints(
        production_system="unknown", setup_cost_mad=None, wage_mad_per_person_day=None
    )
    resolved, evidence, summary = resolve_economics(
        EmptyRepo(),
        SimpleNamespace(project_id="test"),
        SimpleNamespace(zone_id="settat"),
        constraints,
        date(2026, 11, 15),
    )
    assert constraints.sale_price_mad_per_kg == {}  # input is untouched
    assert resolved.budget_mad is None
    assert resolved.sale_price_mad_per_kg["wheat"] == Decimal("4.34")
    assert resolved.yield_kg_per_ha["wheat"] == Decimal("1388.4")
    assert resolved.seasonal_operating_cost_mad_per_ha["wheat"] == 6581
    assert all(
        not row.synthetic and row.source_url for row in evidence if row.origin.value == "observed"
    )
    assert summary["historical"]
    assert any(
        "not a plot forecast" in note for row in summary["references"] for note in row["notes"]
    )


def test_automatic_references_replay_without_changing_money_or_sources():
    args = (EmptyRepo(), SimpleNamespace(project_id="test"), SimpleNamespace(zone_id="settat"))
    first = resolve_economics(*args, FarmConstraints(setup_cost_mad=None), date(2026, 11, 15))
    again = resolve_economics(*args, first[0], date(2026, 11, 15))
    assert first[0] == again[0]
    assert first[2] == again[2]


def test_season_cost_is_not_counted_again_as_unknown_individual_operations():
    constraints, _, _ = resolve_economics(
        EmptyRepo(),
        SimpleNamespace(project_id="test"),
        SimpleNamespace(zone_id="settat"),
        FarmConstraints(setup_cost_mad=None),
        date(2026, 11, 15),
    )
    rotation = Rotation(
        start_date=date(2026, 11, 15),
        end_date=date(2027, 5, 14),
        duration_days=181,
        crops=[
            CropInstance(
                id="w1",
                crop_id="wheat",
                start_date=date(2026, 11, 15),
                end_date=date(2027, 5, 14),
                duration_days=181,
            )
        ],
    )
    _, events, finance, _, _ = build_budget(
        rotation, load_agronomic_catalog(), constraints, Decimal(2), Scenario(), False
    )
    assert finance.whole_plot.operating_cost.value == Decimal("13162.00")
    cost_events = [e for e in events if e.kind == "operating"]
    assert len(cost_events) == 1
    assert cost_events[0].description and cost_events[0].evidence_ids
    assert finance.whole_plot.expected_revenue.value > 0


def test_undated_historical_budgets_do_not_invent_an_observation_year():
    assert all(
        r["year"] is None
        for r in published_records()
        if r["id"].startswith("onca-") and r["crop_id"] in {"faba", "chickpea", "lentil"}
    )


def test_local_producer_observation_wins_and_retail_is_not_a_sale_price():
    class LocalRepo:
        def list_project(self, *args, **kwargs):
            common = dict(
                record_kind="market",
                synthetic=False,
                crop_id="wheat",
                date="2025-11-01",
                locality="Settat",
                source_label="Verified cooperative bulletin",
                source_url="https://example.org/source",
            )
            return [
                {
                    **common,
                    "id": "local-producer",
                    "market_level": "producer",
                    "price_mad_per_kg": "6.25",
                },
                {
                    **common,
                    "id": "retail-not-farmgate",
                    "market_level": "retail",
                    "price_mad_per_kg": "99.00",
                },
            ]

    resolved, evidence, summary = resolve_economics(
        LocalRepo(),
        SimpleNamespace(project_id="test"),
        SimpleNamespace(zone_id="settat"),
        FarmConstraints(setup_cost_mad=None),
        date(2026, 11, 15),
    )
    assert resolved.sale_price_mad_per_kg["wheat"] == Decimal("6.25")
    assert any(e.id == "local-producer" for e in evidence)
    assert not any(e.id == "retail-not-farmgate" for e in evidence)
