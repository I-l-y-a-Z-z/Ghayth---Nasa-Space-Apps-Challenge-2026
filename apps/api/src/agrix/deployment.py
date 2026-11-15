"""Production container startup: validate, migrate, then serve one API worker."""

import logging
import os
from pathlib import Path

from sqlalchemy.engine import make_url

from agrix.config import Settings


def prepare_environment():
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise ValueError("DATABASE_URL is required")
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    url = make_url(raw)
    if url.get_backend_name() == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    settings = Settings.from_env()
    deployment = os.getenv("AGRIX_DEPLOYMENT")
    if deployment in {"cloudflare", "render"}:
        if settings.environment != "production":
            raise ValueError("Hosted deployments require AGRIX_ENV=production")
        if url.get_backend_name() != "postgresql" or url.host in {None, "localhost", "127.0.0.1"}:
            raise ValueError("Hosted deployments require a hosted PostgreSQL/PostGIS database")
        if url.query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
            raise ValueError("Hosted database connections require sslmode=require or stronger")
        if settings.analysis_workers != 1:
            raise ValueError("Hosted deployments require AGRIX_ANALYSIS_WORKERS=1")
    if deployment == "render":
        if os.getenv("AGRIX_SPEECH_ENABLED", "true").lower() not in {"false", "0", "no", "off"}:
            raise ValueError("The free Render deployment requires AGRIX_SPEECH_ENABLED=false")
    if deployment == "cloudflare":
        required = (
            "AGRIX_R2_BUCKET",
            "AGRIX_CACHE_GATEWAY",
            "AGRIX_INTERNAL_TOKEN",
        )
        if not all(os.getenv(name) for name in required):
            raise ValueError(
                "Cloudflare requires its private R2 cache binding and an internal token"
            )
        if os.environ["AGRIX_CACHE_GATEWAY"] != "http://ghayth-cache.internal":
            raise ValueError("Cloudflare requires the private container cache gateway")
        if len(os.environ["AGRIX_INTERNAL_TOKEN"]) < 32:
            raise ValueError("AGRIX_INTERNAL_TOKEN must have at least 32 characters")
    return settings


def main():
    import uvicorn
    from alembic import command
    from alembic.config import Config

    try:
        settings = prepare_environment()
        if make_url(settings.database_url).get_backend_name() == "postgresql":
            configuration = Path(__file__).resolve().parents[2] / "alembic.ini"
            command.upgrade(Config(str(configuration)), "head")
    except Exception as exc:
        logging.error(
            "Container startup failed (%s); check database and deployment settings",
            type(exc).__name__,
        )
        raise SystemExit(1) from None
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        workers=1,
        timeout_graceful_shutdown=30,
    )


if __name__ == "__main__":
    main()
