"""Load only packaged synthetic YAML. The real source registry is not a runtime dependency."""

from importlib.resources import files

import yaml

from agrix.models import Catalog


def load_catalog() -> Catalog:
    raw = files("agrix").joinpath("fixtures/catalog.yaml").read_text(encoding="utf-8")
    return Catalog.model_validate(yaml.safe_load(raw))
