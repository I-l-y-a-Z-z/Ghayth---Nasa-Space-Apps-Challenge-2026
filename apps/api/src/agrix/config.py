"""Explicit server configuration; credentials remain outside browser responses."""

import os
import secrets
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg://agrix:agrix@127.0.0.1:5432/agrix"
    default_analysis_mode: str = "live"
    cache_directory: Path = Path("data/cache")
    network_enabled: bool = True
    analysis_start: date = date(2021, 1, 1)
    analysis_end: date = date(2025, 12, 31)
    environment: str = "development"
    access_password: str | None = None
    session_secret: str = ""
    allowed_projects: tuple[str, ...] = ("demo",)
    map_style_url: str | None = None
    map_tile_url: str | None = None
    geocoder_url: str | None = None
    llm_api_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    analysis_workers: int = 2

    def __post_init__(self):
        if not 1 <= self.analysis_workers <= 4:
            raise ValueError("AGRIX_ANALYSIS_WORKERS must be between 1 and 4")
        if self.default_analysis_mode not in {"live", "demo"}:
            raise ValueError("AGRIX_DEFAULT_ANALYSIS_MODE must be live or demo")
        if self.analysis_end < self.analysis_start:
            raise ValueError("Analysis end must follow start")
        if self.access_password is not None and not self.session_secret:
            raise ValueError("Password-protected workspaces require a session secret")
        if self.environment == "production" and (
            not self.access_password or len(self.session_secret) < 32
        ):
            raise ValueError(
                "Production requires a password and a stable session secret of at least 32 characters"
            )

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            database_url=os.environ.get("DATABASE_URL", cls.database_url),
            default_analysis_mode=os.environ.get("AGRIX_DEFAULT_ANALYSIS_MODE", "live"),
            cache_directory=Path(os.environ.get("AGRIX_CACHE_DIRECTORY", "data/cache")),
            network_enabled=os.environ.get("AGRIX_PROVIDER_NETWORK", "true").lower()
            in {"true", "1", "yes"},
            analysis_start=date.fromisoformat(
                os.environ.get("AGRIX_ANALYSIS_START", f"{date.today().year - 5}-01-01")
            ),
            analysis_end=date.fromisoformat(
                os.environ.get("AGRIX_ANALYSIS_END", f"{date.today().year - 1}-12-31")
            ),
            environment=os.environ.get("AGRIX_ENV", "development"),
            access_password=os.environ.get("AGRIX_ACCESS_PASSWORD") or None,
            session_secret=os.environ.get("AGRIX_SESSION_SECRET") or secrets.token_hex(32),
            allowed_projects=tuple(
                p.strip()
                for p in os.environ.get("AGRIX_ALLOWED_PROJECTS", "demo").split(",")
                if p.strip()
            ),
            map_style_url=os.environ.get("AGRIX_MAP_STYLE_URL") or None,
            map_tile_url=os.environ.get("AGRIX_MAP_TILE_URL") or None,
            geocoder_url=os.environ.get("AGRIX_GEOCODER_URL") or None,
            llm_api_url=os.environ.get("AGRIX_LLM_API_URL") or None,
            llm_api_key=os.environ.get("AGRIX_LLM_API_KEY") or None,
            llm_model=os.environ.get("AGRIX_LLM_MODEL") or None,
            analysis_workers=int(os.environ.get("AGRIX_ANALYSIS_WORKERS", "2")),
        )
        if settings.default_analysis_mode not in {"live", "demo"}:
            raise ValueError("AGRIX_DEFAULT_ANALYSIS_MODE must be live or demo")
        if settings.analysis_end < settings.analysis_start:
            raise ValueError("Analysis end must follow start")
        if settings.environment == "production" and (
            not settings.access_password or not os.environ.get("AGRIX_SESSION_SECRET")
        ):
            raise ValueError("Production requires AGRIX_ACCESS_PASSWORD and AGRIX_SESSION_SECRET")
        return settings
