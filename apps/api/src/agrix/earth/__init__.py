"""Source-aware Earth observation adapters and the live plot-profile pipeline."""

from agrix.earth.manifest import load_assets, register_asset
from agrix.earth.pipeline import PROFILE_VERSION, ProfilePipeline

__all__ = ["PROFILE_VERSION", "ProfilePipeline", "load_assets", "register_asset"]
