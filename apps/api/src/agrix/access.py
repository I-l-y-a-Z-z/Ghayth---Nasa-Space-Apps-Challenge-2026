"""Optional local password gate with signed, expiring project-scoped sessions."""

import base64
import hashlib
import hmac
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

COOKIE_NAME = "agrix_session"
SESSION_SECONDS = 8 * 60 * 60
_PROTECTED = (
    "/workspace",
    "/speech",
    "/plots",
    "/analyses",
    "/rotations",
    "/evaluations",
    "/plans",
    "/chat",
    "/demo",
    "/market-history",
    "/services",
    "/records",
)


def _error(code: str, message: str, status: int) -> JSONResponse:
    response = JSONResponse({"error": {"code": code, "message": message}}, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _signature(payload: str, secret: str) -> str:
    return _encode(hmac.new(secret.encode(), payload.encode("ascii"), hashlib.sha256).digest())


def _mint(secret: str, projects: tuple[str, ...]) -> str:
    payload = _encode(
        json.dumps(
            {
                "version": 1,
                "expires": int(time.time()) + SESSION_SECONDS,
                "projects": list(projects),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    return payload + "." + _signature(payload, secret)


def _verify(token: str | None, secret: str, allowed: tuple[str, ...]) -> list[str] | None:
    if not token or len(token) > 8192:
        return None
    try:
        payload, signature = token.split(".")
        if not hmac.compare_digest(
            signature.encode("ascii"), _signature(payload, secret).encode("ascii")
        ):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if (
            data.get("version") != 1
            or not isinstance(data.get("expires"), int)
            or data["expires"] <= time.time()
            or data["expires"] > time.time() + SESSION_SECONDS + 60
            or not isinstance(data.get("projects"), list)
        ):
            return None
        if not all(isinstance(project, str) for project in data["projects"]):
            return None
        return [project for project in data["projects"] if project in allowed]
    except (ValueError, TypeError, UnicodeError, KeyError, AttributeError):
        return None


def install_access_control(app: FastAPI, settings) -> None:
    """Install once during app creation; credentials never appear in responses."""
    password = settings.access_password
    required = password is not None
    projects = tuple(settings.allowed_projects)
    secret = settings.session_secret
    secure = settings.environment == "production"

    def session(request: Request) -> list[str] | None:
        return (
            _verify(request.cookies.get(COOKIE_NAME), secret, projects)
            if required
            else list(projects)
        )

    @app.middleware("http")
    async def protect_workspace(request: Request, call_next):
        path = request.url.path
        protected = any(path == prefix or path.startswith(prefix + "/") for prefix in _PROTECTED)
        if required and protected:
            scopes = session(request)
            if scopes is None:
                return _error("AUTHENTICATION_REQUIRED", "Sign in to access this workspace", 401)
            project_id = request.headers.get("X-Project-ID", "demo")
            if project_id not in scopes:
                return _error(
                    "PROJECT_ACCESS_DENIED", "This session cannot access that project", 403
                )
        response = await call_next(request)
        if required and protected:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/session", tags=["session"])
    async def current_session(request: Request):
        scopes = session(request)
        response = JSONResponse(
            {
                "authenticated": scopes is not None,
                "required": required,
                "projects": scopes if scopes is not None else [],
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/session", tags=["session"])
    async def login(request: Request):
        # Parse manually so validation errors can never echo submitted passwords.
        try:
            body = await request.body()
            if len(body) > 4096:
                return _error("INVALID_CREDENTIAL_REQUEST", "Supply a password", 422)
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {"password"}:
                return _error("INVALID_CREDENTIAL_REQUEST", "Supply a password", 422)
            candidate = payload["password"]
            if not isinstance(candidate, str) or len(candidate) > 1024:
                return _error("INVALID_CREDENTIAL_REQUEST", "Supply a password", 422)
        except (ValueError, UnicodeError):
            return _error("INVALID_CREDENTIAL_REQUEST", "Supply a password", 422)
        if required and not hmac.compare_digest(candidate.encode(), password.encode()):
            return _error("AUTHENTICATION_FAILED", "Password was not accepted", 401)
        response = JSONResponse(
            {"authenticated": True, "required": required, "projects": list(projects)}
        )
        response.headers["Cache-Control"] = "no-store"
        if required:
            response.set_cookie(
                COOKIE_NAME,
                _mint(secret, projects),
                max_age=SESSION_SECONDS,
                httponly=True,
                secure=secure,
                samesite="strict",
                path="/",
            )
        return response

    @app.delete("/session", tags=["session"])
    async def logout():
        response = JSONResponse(
            {
                "authenticated": not required,
                "required": required,
                "projects": [] if required else list(projects),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(
            COOKIE_NAME, path="/", secure=secure, httponly=True, samesite="strict"
        )
        return response
