"""Ghayth product: original planning engine, integrated web client and Darija speech."""

# Bootstrap repository-local services before importing the application.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "apps/api/src"))
sys.path.insert(0, str(ROOT / "services/speech"))
from agrix.api import create_app
from agrix.config import Settings

from speech_api import install_speech, speech_enabled

settings = Settings.from_env()
app = create_app(
    settings=settings, initialize=settings.database_url.startswith("sqlite")
)
app.title = "Ghayth — farm planning"
install_speech(app)


@app.middleware("http")
async def product_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    microphone = "(self)" if speech_enabled() else "()"
    response.headers["Permissions-Policy"] = (
        f"camera=(), microphone={microphone}, geolocation=(self)"
    )
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith(("/app/", "/assets/")) or request.url.path == "/":
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https://tile.openstreetmap.org; "
            "font-src 'self'; connect-src 'self' https://tile.openstreetmap.org; "
            "worker-src 'self' blob:; media-src 'self' blob:; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
    return response
