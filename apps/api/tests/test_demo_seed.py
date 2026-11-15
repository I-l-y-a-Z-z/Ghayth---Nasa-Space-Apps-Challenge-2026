"""Source-aware sample data and repeatable, project-scoped testing scenarios."""

from collections import Counter
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agrix.catalog import load_catalog
from agrix.db import (
    AnalysisRow,
    EvaluationRow,
    LocalRecordRow,
    PlanRow,
    PlotRow,
    Repository,
    create_schema,
    make_engine,
)
from agrix.demo_seed import SCENARIOS, demo_scenario_ids, load_weather_samples, seed_workspace
from agrix.models import Plan, Plot
from agrix.profiles import get_profile


@pytest.fixture
def demo_engine(tmp_path):
    engine = make_engine("sqlite+pysqlite:///" + str(tmp_path / "demo.sqlite3"))
    create_schema(engine)
    yield engine
    engine.dispose()


def _counts(session):
    return {
        table.__tablename__: session.scalar(select(func.count()).select_from(table))
        for table in (PlotRow, AnalysisRow, EvaluationRow, PlanRow, LocalRecordRow)
    }


def test_seed_commits_realistic_cases_with_all_feasibility_states(demo_engine):
    catalog = load_catalog()
    with Session(demo_engine) as session, session.begin():
        result = seed_workspace(Repository(session), catalog)
    with Session(demo_engine) as session:
        repo = Repository(session)
        outcomes = Counter()
        for scenario, plot_id, plan_id in zip(
            SCENARIOS, result["plot_ids"], result["plan_ids"], strict=True
        ):
            plot = Plot.model_validate(repo.get("plots", plot_id))
            plan = Plan.model_validate(repo.get("plans", plan_id))
            assert abs(plot.area_ha - Decimal(scenario["area_ha"])) < Decimal("0.0001")
            assert plot.constraints.wage_mad_per_person_day == Decimal("180.00")
            assert plan.evaluation_snapshot.status == scenario["expected_status"]
            assert plan.evaluation_snapshot.synthetic is True
            assert plan.assumptions == plot.constraints
            assert plan.status == (
                "draft" if scenario["expected_status"] == "doesnt_fit" else "selected"
            )
            assert all(len(identifier) <= 64 for identifier in (plot_id, plan_id))
            outcomes[plan.evaluation_snapshot.status] += 1
        assert set(outcomes) == {"fits_current_inputs", "needs_confirmation", "doesnt_fit"}
        assert len(result["plot_ids"]) == len(result["plan_ids"]) == 6
        assert result["scenario_ids"] == demo_scenario_ids()
        assert len(result["weather_sample_ids"]) == 3


def test_seed_does_not_overwrite_edits_or_duplicate_evaluations(demo_engine):
    catalog = load_catalog()
    with Session(demo_engine) as session, session.begin():
        repo = Repository(session)
        original = seed_workspace(repo, catalog)
        before = _counts(session)
        plot = repo.get("plots", original["plot_ids"][0])
        plot.update(revision=2, name="Edited by the tester")
        plot["constraints"]["budget_mad"] = "9999.00"
        repo.save_plot(plot)
        plan = repo.get("plans", original["plan_ids"][0])
        original_evaluation = plan["evaluation_snapshot"]
        plan.update(revision=2, name="Tester shortlist")
        repo.put("plans", plan["id"], plan)
    with Session(demo_engine) as session, session.begin():
        repo = Repository(session)
        repeated = seed_workspace(repo, catalog)
        assert repeated == original
        assert _counts(session) == before
        assert repo.get("plots", original["plot_ids"][0])["name"] == "Edited by the tester"
        assert repo.get("plots", original["plot_ids"][0])["constraints"]["budget_mad"] == "9999.00"
        stored = repo.get("plans", original["plan_ids"][0])
        assert stored["name"] == "Tester shortlist"
        assert stored["evaluation_snapshot"] == original_evaluation


def test_projects_have_independent_demo_records_and_shared_public_weather(demo_engine):
    catalog = load_catalog()
    with Session(demo_engine) as session, session.begin():
        repo = Repository(session)
        one = seed_workspace(repo, catalog, "farmers-one")
        two = seed_workspace(repo, catalog, "farmers-two")
        for key in ("plot_ids", "plan_ids", "scenario_ids"):
            assert not set(one[key]) & set(two[key])
        assert one["weather_sample_ids"] == two["weather_sample_ids"]
        assert _counts(session)["plots"] == 12
        for project, result in (("farmers-one", one), ("farmers-two", two)):
            for record_id in result["scenario_ids"]:
                record = repo.get("local_records", record_id)
                assert record["project_id"] == project
                assert record["synthetic"] is True
                assert "assumption" in record["assumptions_note"]
        for record_id in one["weather_sample_ids"]:
            record = repo.get("local_records", record_id)
            assert record["public"] and record["synthetic"] is False
            assert "project_id" not in record


def test_source_samples_preserve_dates_values_and_provenance():
    samples = load_weather_samples()
    assert len(samples) == 3
    assert sum(len(s["rows"]) for s in samples) == 93
    expected = {
        "weather-power-settat-oct2024": Decimal("46.43"),
        "weather-imerg-early-settat-oct2024": Decimal("23.559965"),
        "weather-imerg-late-settat-oct2024": Decimal("28.253339"),
    }
    for sample in samples:
        assert sample["synthetic"] is False
        assert len(sample["sha256"]) == 64
        assert sample["source_filename"]
        assert sample["source_url"].startswith("https://")
        assert sample["period"] == {"start": "2024-10-01", "end": "2024-10-31"}
        assert sample["rows"][0]["date"] == "2024-10-01"
        assert sample["rows"][-1]["date"] == "2024-10-31"
        assert sample["summary"]["missing_values"] == 0
        assert Decimal(str(sample["summary"]["rainfall_total_mm"])) == expected[sample["id"]]
    assert "reanalysis" in " ".join(samples[0]["notes"])
    for sample in samples[1:]:
        assert "not Final" in " ".join(sample["notes"])
        assert "unit label" in " ".join(sample["notes"])
        assert "retrieval-count" in " ".join(sample["notes"])


def test_real_samples_do_not_masquerade_as_annual_profile_inputs(demo_engine):
    with Session(demo_engine) as session, session.begin():
        repo = Repository(session)
        result = seed_workspace(repo, load_catalog())
        plot = Plot.model_validate(repo.get("plots", result["plot_ids"][0]))
        profile = get_profile(plot)
        assert profile.synthetic is True
        assert profile.metrics["rainfall_mm"].value == Decimal("350")
        assert profile.evidence[0].period.start.isoformat() == "2024-10-01"
        assert profile.evidence[0].period.end.isoformat() == "2025-09-30"
        assert all(e.source_id.startswith("fixture-") for e in profile.evidence)
        no_rule_plan = Plan.model_validate(repo.get("plans", result["plan_ids"][3]))
        assert no_rule_plan.evaluation_snapshot.financials.whole_plot.expected_revenue.value is None
        assert no_rule_plan.assumptions.yield_kg_per_ha == {}
