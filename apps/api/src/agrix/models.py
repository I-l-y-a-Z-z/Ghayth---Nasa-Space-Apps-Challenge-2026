"""Versioned public contracts. Decimal money is serialized as MAD strings with two decimals."""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)


def money(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Money must be a finite decimal, not a boolean")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > Decimal("999999999999.99"):
            raise ValueError("Money must be finite and at most 999999999999.99 MAD")
        return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Money must be a finite decimal") from exc


Money = Annotated[
    Decimal,
    BeforeValidator(money),
    PlainSerializer(lambda value: format(value, ".2f"), return_type=str),
]
NonNegativeMoney = Annotated[Money, Field(ge=0)]
Quantity = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=18, decimal_places=6)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class ValidationIssue(Model):
    loc: list[str | int]
    type: str
    message: str


class ErrorBody(Model):
    code: str
    message: str
    details: list[ValidationIssue] | None = None


class ErrorResponse(Model):
    error: ErrorBody


class Availability(str, Enum):
    available = "available"
    partial = "partial"
    missing = "missing"
    unsupported = "unsupported"
    inapplicable = "inapplicable"


class Origin(str, Enum):
    observed = "observed"
    modeled = "modeled"
    farmer_provided = "farmer_provided"
    assumption = "assumption"


class Reason(Model):
    # Stable, language-neutral codes; values have explicit JSON scalar types.
    code: str
    values: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    crop_instance_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class DateRange(Model):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError("end must be on or after start")
        return self


class UnitConversion(Model):
    original_unit: str
    canonical_unit: str
    multiplier: Decimal
    offset: Decimal = Decimal(0)


class Evidence(Model):
    id: str
    source_id: str
    source_version: str
    origin: Origin
    availability: Availability = Availability.available
    synthetic: bool = True
    source_url: str | None = None
    quality_notes: list[str] = Field(default_factory=list)
    uncertainty: str | None = None
    period: DateRange | None = None
    geographic_bounds: tuple[float, float, float, float] | None = None
    resolution_m: float | None = Field(default=None, gt=0)
    depth_cm: tuple[int, int] | None = None
    unit_conversion: UnitConversion | None = None
    reason: Reason | None = None


class Metric(Model):
    value: Decimal | None = None
    unit: str
    availability: Availability
    origin: Origin
    evidence_ids: list[str] = Field(default_factory=list)
    reason: Reason | None = None
    depth_cm: tuple[int, int] | None = None

    @model_validator(mode="after")
    def missing_is_explicit(self) -> "Metric":
        if self.value is not None and not self.value.is_finite():
            raise ValueError("Metric must be finite")
        if self.availability == Availability.available and self.value is None:
            raise ValueError("Available metric requires a value")
        if self.value is None and self.reason is None:
            raise ValueError("Unavailable metric requires a reason")
        return self


class MoneyMetric(Model):
    value: Money | None = None
    unit: Literal["MAD", "MAD/ha"] = "MAD"
    availability: Availability = Availability.available
    origin: Origin = Origin.assumption
    evidence_ids: list[str] = Field(default_factory=lambda: ["fixture-budget-v1"])
    reason: Reason | None = None

    @model_validator(mode="after")
    def explicit_value(self) -> "MoneyMetric":
        if self.value is None and (
            self.reason is None or self.availability == Availability.available
        ):
            raise ValueError("Missing money requires availability and reason")
        return self


T = TypeVar("T")


class Section(Model, Generic[T]):
    availability: Availability = Availability.available
    data: T | None = None
    reason: Reason | None = None

    @model_validator(mode="after")
    def explicit_gap(self) -> "Section[T]":
        if self.data is None and (
            self.reason is None or self.availability == Availability.available
        ):
            raise ValueError("Unavailable section requires availability and reason")
        return self


class Polygon(Model):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[tuple[float, float]]]


class EquipmentAccess(Model):
    mode: Literal["owned", "shared", "rented", "hired_with_operator", "unavailable", "unknown"]
    operator_included: bool | None = None
    cost_mad_per_ha: NonNegativeMoney | None = None
    operator_labor: Literal["paid", "family", "included"] = "paid"


class SoilTest(Model):
    property: str
    value: Decimal = Field(allow_inf_nan=False)
    unit: str
    depth_top_cm: int = Field(ge=0, le=200)
    depth_bottom_cm: int = Field(gt=0, le=200)
    sample_date: date
    source_label: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_depth(self) -> "SoilTest":
        if self.sample_date > date.today():
            raise ValueError("A measured soil sample date cannot be in the future")
        if self.depth_bottom_cm <= self.depth_top_cm:
            raise ValueError("Soil test bottom depth must exceed top depth")
        valid_units = {
            "soil_ph": {"pH", "1"},
            "sand_pct": {"%"},
            "clay_pct": {"%"},
            "silt_pct": {"%"},
            "soil_moisture": {"m3/m3", "%"},
        }
        if self.property not in valid_units or self.unit not in valid_units[self.property]:
            raise ValueError("Use a supported soil property and its declared scientific unit")
        maximum = 14 if self.property == "soil_ph" else 1 if self.unit == "m3/m3" else 100
        if not 0 <= self.value <= maximum:
            raise ValueError("Soil test value is outside its scientific range")
        return self


class OperationCostOverride(Model):
    inputs_mad_per_ha: NonNegativeMoney | None = None
    machinery_mad_per_ha: NonNegativeMoney | None = None
    paid_person_days_per_ha: Quantity | None = None
    family_person_days_per_ha: Quantity | None = None
    operator_days_per_ha: Quantity | None = None


class AdditionalCost(Model):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    date: date
    amount_mad: NonNegativeMoney
    kind: Literal["setup", "operating"] = "operating"


class FarmConstraints(Model):
    budget_mad: NonNegativeMoney | None = None
    production_system: Literal["rainfed", "irrigated", "unknown"] = "rainfed"
    irrigation_reliability: Literal["reliable", "unreliable", "unknown"] = "unknown"
    irrigation_mm_by_date: dict[date, Quantity] = Field(default_factory=dict)
    irrigation_source_label: str | None = Field(default=None, max_length=300)
    irrigation_verified_on: date | None = None
    machinery_access: Literal["available", "unavailable", "unknown"] = "unknown"
    labor_person_days_available: Quantity | None = None
    labor_by_month: dict[str, Quantity] = Field(default_factory=dict)
    previous_crops: list[str] | None = None
    priorities: list[str] = Field(default_factory=list)
    sale_price_mad_per_kg: dict[str, NonNegativeMoney] = Field(default_factory=dict)
    yield_kg_per_ha: dict[str, Quantity] = Field(default_factory=dict)
    seasonal_operating_cost_mad_per_ha: dict[str, NonNegativeMoney] = Field(default_factory=dict)
    economic_evidence_ids: list[str] = Field(default_factory=list)
    # Explicit synthetic budgeting defaults, editable through the same evaluation path.
    setup_cost_mad: NonNegativeMoney | None = Decimal("200.00")
    wage_mad_per_person_day: NonNegativeMoney | None = Decimal("100.00")
    payment_lag_days: int = Field(default=30, ge=0, le=365)
    equipment: dict[str, EquipmentAccess] = Field(default_factory=dict)
    soil_tests: list[SoilTest] = Field(default_factory=list)
    operation_cost_overrides: dict[str, OperationCostOverride] = Field(default_factory=dict)
    additional_costs: list[AdditionalCost] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_additional_cost_ids(self) -> "FarmConstraints":
        if self.irrigation_verified_on and self.irrigation_verified_on > date.today():
            raise ValueError("Irrigation verification cannot be in the future")
        if self.irrigation_mm_by_date and not self.irrigation_source_label:
            raise ValueError(
                "A dated irrigation schedule requires an explicit source/assumption label"
            )
        if len({c.id for c in self.additional_costs}) != len(self.additional_costs):
            raise ValueError("Additional cost IDs must be unique")
        return self


class PlotCreate(Model):
    name: str = Field(default="My plot", min_length=1, max_length=200)
    geometry: Polygon
    constraints: FarmConstraints = Field(default_factory=FarmConstraints)
    analysis_mode: Literal["demo", "live"] | None = None


class PlotPatch(Model):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    geometry: Polygon | None = None
    constraints: FarmConstraints | None = None

    @model_validator(mode="after")
    def has_changes(self) -> "PlotPatch":
        if all(getattr(self, k) is None for k in ("name", "geometry", "constraints")):
            raise ValueError("Supply name, geometry, or constraints to update")
        return self


class Plot(Model):
    id: str
    project_id: str
    name: str
    revision: int = Field(ge=1)
    geometry: Polygon
    area_ha: Quantity
    constraints: FarmConstraints
    created_at: datetime
    updated_at: datetime
    analysis_mode: Literal["demo", "live"] = "demo"


class LayerCoverage(Model):
    id: str
    availability: Availability
    evidence_ids: list[str]
    reason: Reason | None = None
    state: Literal["pending", "running", "complete", "partial", "failed", "unsupported"] = (
        "complete"
    )
    attempts: int = 0
    updated_at: datetime | None = None


class DailyWeather(Model):
    date: date
    rain_mm: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    tmin_c: Decimal | None = Field(default=None, allow_inf_nan=False)
    tmax_c: Decimal | None = Field(default=None, allow_inf_nan=False)
    tmean_c: Decimal | None = Field(default=None, allow_inf_nan=False)
    humidity_pct: Decimal | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    wind_m_s: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    solar_mj_m2: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    root_zone_moisture: Decimal | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    surface_moisture: Decimal | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    evidence_ids: list[str] = Field(default_factory=list)


class AssetReference(Model):
    id: str
    dataset_id: str
    path: str
    sha256: str
    format: str
    period: DateRange | None = None
    variables: list[str] = Field(default_factory=list)


class PlotProfile(Model):
    plot_id: str
    plot_revision: int
    fixture_version: str
    zone_id: str | None
    rule_coverage: Availability
    metrics: dict[str, Metric]
    layers: list[LayerCoverage]
    evidence: list[Evidence]
    synthetic: bool = True
    weather: list[DailyWeather] = Field(default_factory=list)
    assets: list[AssetReference] = Field(default_factory=list)
    water_availability: dict = Field(default_factory=dict)


class AnalysisRequest(Model):
    plot_revision: int | None = Field(default=None, ge=1)
    start_date: date | None = None
    end_date: date | None = None
    retry_layers: (
        list[
            Literal[
                "rainfall",
                "temperature",
                "soil_properties",
                "soil_moisture",
                "terrain",
                "vegetation",
            ]
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def ordered_period(self) -> "AnalysisRequest":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("Analysis end_date must follow start_date")
        return self


class Analysis(Model):
    id: str
    project_id: str
    plot_id: str
    plot_revision: int
    fixture_version: str
    status: Literal["pending", "running", "complete", "partial", "unsupported", "failed"]
    profile: PlotProfile | None = None
    created_at: datetime
    synthetic: bool = True
    request_key: str | None = None
    period: DateRange | None = None
    completed_at: datetime | None = None
    error: Reason | None = None
    plot_snapshot: Plot | None = None
    previous_analysis_id: str | None = None
    retry_layers: list[str] | None = None


class OperationTemplate(Model):
    id: str
    offset_days: int = Field(ge=0)
    inputs_mad_per_ha: NonNegativeMoney = Decimal(0)
    machinery_mad_per_ha: NonNegativeMoney = Decimal(0)
    machinery_id: str | None = None
    machinery_mode: Literal["none", "owned", "shared", "rented", "hired_with_operator"] = "none"
    paid_person_days_per_ha: Quantity = Decimal(0)
    family_person_days_per_ha: Quantity = Decimal(0)
    included_operator_person_days_per_ha: Quantity = Decimal(0)
    costs_known: bool = True
    labor_known: bool = True
    quantities: list[dict] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def operator_is_not_paid_twice(self) -> "OperationTemplate":
        if (
            self.included_operator_person_days_per_ha
            and self.machinery_mode != "hired_with_operator"
        ):
            raise ValueError("Included operator requires hired_with_operator machinery")
        return self


class EnvironmentalParameters(Model):
    """Versioned screening parameters; thermal targets remain explicit assumptions."""

    temperature_c: tuple[float, float, float, float]
    ph: tuple[float, float, float, float]
    cycle_days: tuple[int, int]
    kc: tuple[float, float, float]
    root_depth_m: float = Field(gt=0, le=3, allow_inf_nan=False)
    depletion_fraction: float = Field(gt=0, lt=1, allow_inf_nan=False)
    thermal_base_c: float = Field(allow_inf_nan=False)
    thermal_upper_c: float = Field(allow_inf_nan=False)
    thermal_target_gdd: float = Field(gt=0, allow_inf_nan=False)
    thermal_target_range: tuple[float, float]
    preferred_texture: list[Literal["light", "medium", "heavy"]]
    altitude_max_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    source_ids: list[str]

    @model_validator(mode="after")
    def ordered_ranges(self):
        import math

        for values in (self.temperature_c, self.ph, self.thermal_target_range):
            if not all(math.isfinite(x) for x in values) or list(values) != sorted(values):
                raise ValueError("Environmental ranges must be finite and ordered")
        if not 0 <= self.ph[0] <= self.ph[-1] <= 14:
            raise ValueError("pH range must be within 0..14")
        if not 1 <= self.cycle_days[0] <= self.cycle_days[1] <= 730:
            raise ValueError("Crop cycle range must be ordered within 1..730 days")
        if not self.thermal_base_c < self.thermal_upper_c:
            raise ValueError("Thermal upper temperature must exceed the base")
        if (
            not self.thermal_target_range[0]
            <= self.thermal_target_gdd
            <= self.thermal_target_range[1]
        ):
            raise ValueError("Thermal target must lie within its sensitivity range")
        if any(not math.isfinite(x) or not 0 <= x <= 2 for x in self.kc):
            raise ValueError("Crop coefficients must be within 0..2")
        return self


class CropProfile(Model):
    id: str
    label_key: str
    group: str
    zone_ids: list[str]
    production_systems: list[str]
    sowing_months: list[int] = Field(min_length=1)
    duration_days: int = Field(ge=1, le=730)
    forbidden_predecessors: list[str] = Field(default_factory=list)
    yield_kg_per_ha: Quantity
    sale_price_mad_per_kg: NonNegativeMoney
    operations: list[OperationTemplate]
    synthetic: bool = True
    family: str | None = None
    sowing_windows: list[dict] = Field(default_factory=list)
    duration_min_days: int | None = Field(default=None, ge=1)
    duration_max_days: int | None = Field(default=None, ge=1)
    water_min_mm: Decimal | None = Field(default=None, ge=0)
    water_max_mm: Decimal | None = Field(default=None, ge=0)
    ph_min: Decimal | None = None
    ph_max: Decimal | None = None
    heat_threshold_c: Decimal | None = None
    frost_threshold_c: Decimal | None = None
    sensitive_stages: list[dict] = Field(default_factory=list)
    references: list[dict] = Field(default_factory=list)
    geographic_coverage: list[dict] = Field(default_factory=list)
    fallow: bool = False
    budget_yield_status: str = "assumption"
    environmental_parameters: EnvironmentalParameters | None = None

    @model_validator(mode="after")
    def valid_template(self) -> "CropProfile":
        if any(m < 1 or m > 12 for m in self.sowing_months):
            raise ValueError("Sowing months must be 1..12")
        if len({o.id for o in self.operations}) != len(self.operations):
            raise ValueError("Operation IDs must be unique within a crop")
        if any(o.offset_days >= self.duration_days for o in self.operations):
            raise ValueError("Operation offset must fit crop duration")
        return self


class Catalog(Model):
    version: str
    fixture_version: str
    synthetic: bool = True
    crops: list[CropProfile] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_ids(self) -> "Catalog":
        if len({c.id for c in self.crops}) != len(self.crops):
            raise ValueError("Crop IDs must be unique")
        return self


class CropSelection(Model):
    crop_id: str
    start_date: date | None = None
    end_date: date | None = None
    date_policy: Literal["auto", "explicit", "environmental"] = "auto"

    @model_validator(mode="after")
    def order_dates(self) -> "CropSelection":
        if self.end_date is not None and self.start_date is None:
            raise ValueError("end_date requires start_date")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class CropInstance(Model):
    id: str
    crop_id: str
    start_date: date
    end_date: date
    duration_days: int = Field(ge=1)
    timing: dict = Field(default_factory=dict)


class Rotation(Model):
    crops: list[CropInstance] = Field(min_length=1)
    start_date: date
    end_date: date
    duration_days: int = Field(ge=1)


class Scenario(Model):
    id: str = "fixture-baseline-v1"
    label_key: str = "scenario.synthetic_baseline"
    yield_multiplier: Quantity = Decimal(1)
    price_multiplier: Quantity = Decimal(1)
    evidence_ids: list[str] = Field(
        default_factory=lambda: ["fixture-scenario-v1"], min_length=1, max_length=1
    )
    synthetic: bool = True
    historical_years: list[int] = Field(default_factory=list, max_length=30)
    rain_onset_threshold_mm: Quantity = Decimal("20")
    dry_day_threshold_mm: Quantity = Decimal("1")

    @model_validator(mode="after")
    def historical_calendar_range(self) -> "Scenario":
        if any(year < 1 or year > 9989 for year in self.historical_years):
            raise ValueError("Historical years must be between 1 and 9989")
        return self


class EvaluationRequest(Model):
    plot_id: str
    plot_revision: int = Field(ge=1)
    start_date: date
    horizon_days: int = Field(default=730, ge=1, le=3650)
    sequence: list[CropSelection] = Field(min_length=1, max_length=20)
    constraints: FarmConstraints | None = None
    scenario: Scenario = Field(default_factory=Scenario)

    @model_validator(mode="after")
    def calendar_range(self) -> "EvaluationRequest":
        if self.start_date.year > 9990:
            raise ValueError("Planning start must be before year 9991")
        return self


class SuggestionRequest(Model):
    plot_id: str
    plot_revision: int = Field(ge=1)
    start_date: date
    horizon_days: int = Field(default=730, ge=1, le=3650)
    constraints: FarmConstraints | None = None
    scenario: Scenario = Field(default_factory=Scenario)

    @model_validator(mode="after")
    def calendar_range(self) -> "SuggestionRequest":
        if self.start_date.year > 9990:
            raise ValueError("Planning start must be before year 9991")
        return self


class Check(Model):
    outcome: Literal["pass", "unknown", "fail"]
    reason: Reason


class Operation(Model):
    id: str
    crop_instance_id: str
    code: str
    date: date
    booking_window: DateRange
    area_ha: Quantity
    inputs_cost_mad: NonNegativeMoney | None
    machinery_cost_mad: NonNegativeMoney | None
    paid_labor_cost_mad: NonNegativeMoney | None
    total_cash_cost_mad: NonNegativeMoney | None
    paid_person_days: Quantity | None
    family_person_days: Quantity | None
    included_operator_person_days: Quantity | None
    machinery_id: str | None
    machinery_mode: Literal[
        "none", "owned", "shared", "rented", "hired_with_operator", "unknown", "unavailable"
    ]
    evidence_ids: list[str] = Field(default_factory=lambda: ["fixture-catalog-v1"])
    synthetic: bool = True
    quantities: list[dict] = Field(default_factory=list)


class CashEvent(Model):
    id: str
    date: date
    direction: Literal["inflow", "outflow"]
    kind: Literal["setup", "operating", "sale"]
    amount_mad: NonNegativeMoney | None
    availability: Availability = Availability.available
    reason: Reason | None = None
    crop_instance_id: str | None = None
    operation_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=lambda: ["fixture-budget-v1"])
    synthetic: bool = True
    description: str | None = None


class FinancialTotals(Model):
    initial_investment: MoneyMetric
    operating_cost: MoneyMetric
    expected_revenue: MoneyMetric
    operating_margin: MoneyMetric
    net_cash_flow: MoneyMetric
    cash_before_first_receipt: MoneyMetric
    peak_funding_need: MoneyMetric


class Financials(Model):
    whole_plot: FinancialTotals
    per_ha: FinancialTotals
    first_receipt_date: date | None
    last_cash_event_date: date | None
    break_even_yield: Metric
    break_even_price: Metric | None = None


class MarketObservation(Model):
    crop_id: str
    date: date
    price_mad_per_kg: NonNegativeMoney
    market_level: Literal[
        "synthetic_producer", "producer", "farm_gate", "wholesale", "retail", "benchmark"
    ] = "synthetic_producer"
    locality: str
    evidence_ids: list[str] = Field(default_factory=lambda: ["fixture-market-v1"])
    synthetic: bool = True
    source_url: str | None = None
    source_label: str | None = None


class Supplier(Model):
    id: str
    name: str
    service: str
    locality: str
    verification_date: date
    contact: str | None = None
    synthetic: bool = True
    geographic_bounds: tuple[float, float, float, float] | None = None
    source_url: str | None = None


class MachineryRequirement(Model):
    operation_id: str
    machinery_id: str
    mode: str
    operator_included: bool
    cost_mad: NonNegativeMoney | None


class LaborSummary(Model):
    paid_person_days: Quantity | None
    family_person_days: Quantity | None
    included_operator_person_days: Quantity | None
    paid_cost_mad: NonNegativeMoney | None
    farm_person_days_by_month: dict[str, Quantity | None]


class EvaluationInputs(Model):
    request: EvaluationRequest
    area_ha: Quantity
    profile: PlotProfile
    catalog: Catalog


class Evaluation(Model):
    id: str
    project_id: str
    plot_id: str
    plot_revision: int
    analysis_id: str
    fixture_version: str
    catalog_version: str
    fingerprint: str
    created_at: datetime
    synthetic: bool = True
    status: Literal["fits_current_inputs", "needs_confirmation", "doesnt_fit"]
    rotation: Rotation
    checks: list[Check]
    pros: list[Reason]
    cons: list[Reason]
    timeline: Section[list[Operation]]
    cash_events: list[CashEvent]
    financials: Financials
    market_history: Section[list[MarketObservation]]
    suppliers: Section[list[Supplier]]
    machinery: Section[list[MachineryRequirement]]
    labor: Section[LaborSummary]
    evidence: list[Evidence]
    input_snapshot: EvaluationInputs
    climate_scenarios: list[dict] = Field(default_factory=list)
    comparison_period: DateRange | None = None
    next_actions: list[Reason] = Field(default_factory=list)
    environmental: dict | None = None
    economics: dict | None = None


class Suggestions(Model):
    candidates: list[Evaluation]
    reasons: list[Reason] = Field(default_factory=list)
    synthetic: bool = True


class PlanCreate(Model):
    plot_id: str
    name: str = Field(default="My plan", min_length=1, max_length=200)
    status: Literal["draft", "selected"] = "draft"
    evaluation_id: str | None = Field(default=None, min_length=1, max_length=64)
    draft: EvaluationRequest | None = None

    @model_validator(mode="after")
    def valid_state(self) -> "PlanCreate":
        if self.status == "selected" and self.evaluation_id is None:
            raise ValueError("A selected plan requires an evaluation")
        if self.evaluation_id is None and self.draft is None:
            raise ValueError("Supply an evaluation or a draft")
        if self.evaluation_id is not None and self.draft is not None:
            raise ValueError("Supply either an evaluation or a draft, not both")
        return self


class PlanPatch(Model):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["draft", "selected"] | None = None
    evaluation_id: str | None = Field(default=None, min_length=1, max_length=64)
    draft: EvaluationRequest | None = None

    @model_validator(mode="after")
    def valid_patch(self) -> "PlanPatch":
        if all(getattr(self, key) is None for key in ("name", "status", "evaluation_id", "draft")):
            raise ValueError("Supply a plan change")
        if self.evaluation_id is not None and self.draft is not None:
            raise ValueError("Supply either an evaluation or a draft, not both")
        return self


class Plan(Model):
    id: str
    project_id: str
    plot_id: str
    plot_revision: int
    revision: int
    name: str
    status: Literal["draft", "selected"]
    evaluation_id: str | None
    evaluation_snapshot: Evaluation | None
    assumptions: FarmConstraints
    draft: EvaluationRequest | None
    created_at: datetime
    updated_at: datetime
    synthetic: bool = True


class ChatRequest(Model):
    plot_id: str
    evaluation_ids: list[str] = Field(default_factory=list, max_length=2)
    message: str = Field(min_length=1, max_length=4000)
    locale: Literal["ary", "fr", "en"] = "ary"
    conversation_id: str | None = None


class ChatReply(Model):
    message: str
    locale: Literal["ary", "fr", "en"]
    evaluation_ids: list[str]
    facts: list[dict[str, str | int | None]]
    evidence_ids: list[str]
    read_only: Literal[True] = True
    synthetic: bool = True
    reason: Reason
    what_if: Evaluation | None = None
    requires_confirmation: bool = False
    suggested_actions: list[dict] = Field(default_factory=list)


class PlanSummary(Model):
    id: str
    name: str
    plot_id: str
    plot_revision: int
    revision: int
    status: Literal["draft", "selected"]
    evaluation_id: str | None
    evaluation_status: Literal["fits_current_inputs", "needs_confirmation", "doesnt_fit"] | None
    updated_at: datetime


class DemoScenario(Model):
    plot_id: str
    title: str
    region: str
    description: str
    expected_status: Literal["fits_current_inputs", "needs_confirmation", "doesnt_fit"]
    assumptions_note: str
    benchmark_source: dict[str, str] = Field(default_factory=dict)


class WeatherVariable(Model):
    id: str
    label: str
    unit: str


class WeatherRow(Model):
    date: date
    values: dict[str, float | None]


class WeatherSample(Model):
    id: str
    title: str
    provider: str
    source_url: str
    period: DateRange
    sample_type: Literal["point", "area_average"]
    coordinates: dict[str, float] | None = None
    bounds: tuple[float, float, float, float] | None = None
    variables: list[WeatherVariable]
    rows: list[WeatherRow]
    summary: dict[str, float | None]
    notes: list[str]
    source_filename: str
    sha256: str
    synthetic: Literal[False] = False


class Workspace(Model):
    plots: list[Plot]
    plans: list[PlanSummary]
    scenarios: list[DemoScenario]
    weather_samples: list[WeatherSample]
    catalog: Catalog
