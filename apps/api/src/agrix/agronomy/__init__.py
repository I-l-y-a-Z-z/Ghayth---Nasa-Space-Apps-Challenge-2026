"""Evidence-qualified Moroccan agronomy and historical climate diagnostics."""

from .catalog import load_agronomic_catalog, reference_evidence, resolve_rule_coverage
from .climate import climate_exposure
from .planning import generate_candidates, maturity_dates, rule_checks

__all__ = [
    "load_agronomic_catalog",
    "reference_evidence",
    "resolve_rule_coverage",
    "climate_exposure",
    "generate_candidates",
    "maturity_dates",
    "rule_checks",
]
