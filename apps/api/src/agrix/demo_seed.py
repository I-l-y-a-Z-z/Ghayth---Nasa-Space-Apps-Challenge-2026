"""Repeatable Morocco-oriented test scenarios and separately sourced weather context.

Farm geometry, crop yields, costs, constraints and plans are demo assumptions.
Packaged weather samples preserve actual supplied NASA values and never replace
the fixture profile or its annual period in the Part 1 evaluator.
"""

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from importlib.resources import files
from math import sqrt

from pyproj import Geod

from agrix.db import Repository
from agrix.geometry import normalize_geometry
from agrix.models import Catalog, FarmConstraints, Plan, Plot, Polygon, SuggestionRequest
from agrix.service import Service

DEMO_DATA_VERSION = "morocco-test-scenarios-1.0.0"
WEATHER_RECORD_IDS = (
    "weather-power-settat-oct2024",
    "weather-imerg-early-settat-oct2024",
    "weather-imerg-late-settat-oct2024",
)
WHEAT_BENCHMARK = {
    "title": "ONICL 2025 domestic soft-wheat marketing circular",
    "url": (
        "https://www.fncl.ma/download/128/circulaires-onicl/45030/"
        "circulaire-n910-onicl-du-21-05-2025-commercialisation-des-cereales-"
        "de-la-production-nationale-2025.pdf"
    ),
    "publication_date": "2025-05-21",
    "reference_price_mad_per_quintal": "280.00",
    "converted_reference_mad_per_kg": "2.80",
    "scope": (
        "Historical 2025 target for standard-quality domestic soft wheat, including charges "
        "and margins associated with purchase and delivery to industrial mills. It is not "
        "a verified farm-gate quote or a future selling-price forecast."
    ),
}
ASSUMPTIONS_NOTE = (
    "Invented field boundaries and editable planning assumptions. Wheat's 2.80 MAD/kg "
    "starting value uses a qualified historical 2025 reference benchmark; barley/faba "
    "prices, yields, wage, budget, payment terms and operations are unverified demo "
    "assumptions. The Part 1 operation template is not a fully costed production budget."
)

# These are deliberately fictional fields near the named localities, not surveyed farms.
SCENARIOS = (
    {
        "key": "chaouia-balanced",
        "title": "Chaouia — balanced rainfed farm",
        "region": "Settat · Chaouia",
        "description": "A 5 ha cereal–legume rotation with hired machinery and sufficient cash.",
        "longitude": -7.62,
        "latitude": 32.98,
        "area_ha": "5",
        "budget_mad": "40000.00",
        "setup_cost_mad": "1500.00",
        "payment_lag_days": 30,
        "machinery_access": "available",
        "expected_status": "fits_current_inputs",
    },
    {
        "key": "chaouia-cash-limited",
        "title": "Chaouia — cash needed before harvest",
        "region": "Settat · Chaouia",
        "description": "The same 5 ha scale with a 12,000 MAD budget: test the funding gap.",
        "longitude": -7.57,
        "latitude": 32.96,
        "area_ha": "5",
        "budget_mad": "12000.00",
        "setup_cost_mad": "1500.00",
        "payment_lag_days": 30,
        "machinery_access": "available",
        "expected_status": "doesnt_fit",
    },
    {
        "key": "saiss-soil-test",
        "title": "Saïss — soil test to confirm",
        "region": "Meknes · Saïss",
        "description": "A 3 ha field whose missing pH evidence keeps the plan provisional.",
        "longitude": -5.58,
        "latitude": 33.90,
        "area_ha": "3",
        "budget_mad": "24000.00",
        "setup_cost_mad": "1800.00",
        "payment_lag_days": 30,
        "machinery_access": "available",
        "expected_status": "needs_confirmation",
    },
    {
        "key": "ouarzazate-rules-gap",
        "title": "Ouarzazate — local crop rules needed",
        "region": "Ouarzazate",
        "description": "A 2 ha example with environmental context but no applicable crop rule.",
        "longitude": -6.90,
        "latitude": 30.96,
        "area_ha": "2",
        "budget_mad": "22000.00",
        "setup_cost_mad": "1500.00",
        "payment_lag_days": 30,
        "machinery_access": "available",
        "expected_status": "needs_confirmation",
    },
    {
        "key": "chaouia-machinery",
        "title": "Chaouia — machinery unavailable",
        "region": "Settat · Chaouia",
        "description": "A 4 ha plan with enough cash but no confirmed contractor access.",
        "longitude": -7.56,
        "latitude": 33.02,
        "area_ha": "4",
        "budget_mad": "30000.00",
        "setup_cost_mad": "2200.00",
        "payment_lag_days": 30,
        "machinery_access": "unavailable",
        "expected_status": "doesnt_fit",
    },
    {
        "key": "chaouia-late-payment",
        "title": "Chaouia — buyer pays after 240 days",
        "region": "Settat · Chaouia",
        "description": "A 5 ha rotation whose late buyer payment overlaps next season's costs.",
        "longitude": -7.65,
        "latitude": 32.94,
        "area_ha": "5",
        "budget_mad": "45000.00",
        "setup_cost_mad": "1500.00",
        "payment_lag_days": 240,
        "machinery_access": "available",
        "expected_status": "doesnt_fit",
    },
)


def _project_key(project_id: str) -> str:
    return hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]


def demo_scenario_ids(project_id: str = "demo") -> list[str]:
    """Return stable project-scoped local-record IDs without reading the database."""
    return [f"scenario-{_project_key(project_id)}-{s['key']}" for s in SCENARIOS]


def load_weather_samples() -> list[dict]:
    """Load packaged, source-identified observations; never read raw provider files at runtime."""
    data = json.loads(
        files("agrix").joinpath("fixtures/weather_samples.json").read_text(encoding="utf-8")
    )
    return data["samples"]


def _field(longitude: float, latitude: float, area_ha: str) -> tuple[Polygon, Decimal]:
    half_diagonal = sqrt(float(area_ha) * 10000 / 2)
    geod = Geod(ellps="WGS84")
    ring = [
        geod.fwd(longitude, latitude, azimuth, half_diagonal)[:2] for azimuth in (225, 135, 45, 315)
    ]
    ring.append(ring[0])
    return normalize_geometry(Polygon(coordinates=[ring]))


def _constraints(scenario: dict) -> FarmConstraints:
    # A rule gap must not be hidden by invented plot-specific yield/price overrides.
    has_rule = scenario["key"] != "ouarzazate-rules-gap"
    return FarmConstraints(
        budget_mad=scenario["budget_mad"],
        production_system="rainfed",
        irrigation_reliability="unknown",
        machinery_access=scenario["machinery_access"],
        labor_person_days_available=Decimal("120"),
        previous_crops=[],
        priorities=["cash_flow", "labor"],
        setup_cost_mad=scenario["setup_cost_mad"],
        wage_mad_per_person_day=Decimal("180.00"),
        payment_lag_days=scenario["payment_lag_days"],
        sale_price_mad_per_kg={"wheat_demo": "2.80", "barley_demo": "3.00", "faba_demo": "7.00"}
        if has_rule
        else {},
        yield_kg_per_ha={"wheat_demo": "2800", "barley_demo": "2200", "faba_demo": "1400"}
        if has_rule
        else {},
    )


def seed_workspace(repository: Repository, catalog: Catalog, project_id: str = "demo") -> dict:
    """Populate a workspace once; caller owns transaction and commit.

    Repeating this function does not replace existing fields, plans, evaluations or
    user edits. Public NASA samples are shared source records; plot/plan/scenario
    identities are scoped to the requested project.
    """
    project_key = _project_key(project_id)
    now = datetime.now(timezone.utc)
    service = Service(repository, catalog, project_id)
    plot_ids, plan_ids, scenario_ids = [], [], []
    for scenario in SCENARIOS:
        suffix = f"{project_key}-{scenario['key']}"
        plot_id = f"demo-p-{suffix}"
        plan_id = f"demo-plan-{suffix}"
        scenario_id = f"scenario-{suffix}"
        payload = repository.get("plots", plot_id)
        if payload is None:
            geometry, area = _field(
                scenario["longitude"], scenario["latitude"], scenario["area_ha"]
            )
            plot = Plot(
                id=plot_id,
                project_id=project_id,
                name=scenario["title"],
                revision=1,
                geometry=geometry,
                area_ha=area,
                constraints=_constraints(scenario),
                created_at=now,
                updated_at=now,
            )
            repository.save_plot(plot.model_dump(mode="json"))
        else:
            plot = Plot.model_validate(payload)
            if plot.project_id != project_id:
                raise ValueError("Demo plot identity belongs to another project")
        existing_plan = repository.get("plans", plan_id)
        if existing_plan is None:
            suggestions = service.suggest(
                SuggestionRequest(
                    plot_id=plot.id,
                    plot_revision=plot.revision,
                    start_date=date(2026, 11, 1),
                    horizon_days=730,
                )
            ).candidates
            evaluation = next(
                item
                for item in suggestions
                if [crop.crop_id for crop in item.rotation.crops][:2] == ["wheat_demo", "faba_demo"]
            )
            plan = Plan(
                id=plan_id,
                project_id=project_id,
                plot_id=plot.id,
                plot_revision=plot.revision,
                revision=1,
                name=f"{scenario['title']} · wheat / faba",
                status="draft" if evaluation.status == "doesnt_fit" else "selected",
                evaluation_id=evaluation.id,
                evaluation_snapshot=evaluation,
                assumptions=evaluation.input_snapshot.request.constraints,
                draft=None,
                created_at=now,
                updated_at=now,
            )
            repository.put("plans", plan_id, plan.model_dump(mode="json"))
        elif existing_plan.get("project_id") != project_id:
            raise ValueError("Demo plan identity belongs to another project")
        if repository.get("local_records", scenario_id) is None:
            repository.put(
                "local_records",
                scenario_id,
                {
                    "id": scenario_id,
                    "kind": "demo_scenario",
                    "record_kind": "demo_scenario",
                    "version": DEMO_DATA_VERSION,
                    "project_id": project_id,
                    "plot_id": plot_id,
                    "plan_id": plan_id,
                    "title": scenario["title"],
                    "region": scenario["region"],
                    "description": scenario["description"],
                    "expected_status": scenario["expected_status"],
                    "assumptions_note": ASSUMPTIONS_NOTE,
                    "benchmark_source": WHEAT_BENCHMARK,
                    "synthetic": True,
                },
            )
        plot_ids.append(plot_id)
        plan_ids.append(plan_id)
        scenario_ids.append(scenario_id)
    weather_ids = []
    for sample in load_weather_samples():
        repository.put(
            "local_records",
            sample["id"],
            {
                **sample,
                "record_kind": "weather_sample",
                "public": True,
            },
        )
        weather_ids.append(sample["id"])
    return {
        "version": DEMO_DATA_VERSION,
        "project_id": project_id,
        "plot_ids": plot_ids,
        "plan_ids": plan_ids,
        "scenario_ids": scenario_ids,
        "weather_sample_ids": weather_ids,
    }
