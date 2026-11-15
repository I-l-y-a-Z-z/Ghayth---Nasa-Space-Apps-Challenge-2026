from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agrix.access import COOKIE_NAME, SESSION_SECONDS, _mint, install_access_control


def _app(password="test-password", environment="development"):
    app = FastAPI()
    settings = SimpleNamespace(
        access_password=password,
        session_secret="test-secret-stable-long-value",
        environment=environment,
        allowed_projects=("demo", "farm"),
    )
    install_access_control(app, settings)

    @app.get("/workspace")
    @app.get("/plots/one")
    @app.get("/records")
    @app.get("/health")
    @app.get("/catalog")
    @app.get("/")
    def route():
        return {"ok": True}

    return app, settings


def test_local_mode_keeps_project_header_development_workflow():
    app, _ = _app(password=None)
    with TestClient(app) as client:
        assert (
            client.get(
                "/workspace", headers={"X-Project-ID": "any-development-project"}
            ).status_code
            == 200
        )
        assert client.get("/session").json() == {
            "authenticated": True,
            "required": False,
            "projects": ["demo", "farm"],
        }


def test_login_required_but_bootstrap_resources_remain_public():
    app, _ = _app()
    with TestClient(app) as client:
        for path in ("/workspace", "/plots/one", "/records"):
            assert client.get(path).status_code == 401
        for path in ("/", "/health", "/catalog", "/session"):
            assert client.get(path).status_code == 200
        assert client.get("/session").json()["authenticated"] is False
        assert client.post("/session", json={"password": "wrong"}).status_code == 401
        response = client.post("/session", json={"password": "test-password"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "test-password" not in cookie
        assert client.get("/workspace").status_code == 200
        assert client.get("/workspace", headers={"X-Project-ID": "farm"}).status_code == 200
        assert client.get("/workspace", headers={"X-Project-ID": "someone-else"}).status_code == 403
        assert client.delete("/session").status_code == 200
        assert client.get("/workspace").status_code == 401


def test_production_cookie_is_secure():
    app, _ = _app(environment="production")
    with TestClient(app, base_url="https://example.test") as client:
        response = client.post("/session", json={"password": "test-password"})
        assert "; Secure" in response.headers["set-cookie"]
        assert client.get("/workspace").status_code == 200


def test_tampered_malformed_and_expired_cookies_are_rejected(monkeypatch):
    app, settings = _app()
    token = _mint(settings.session_secret, settings.allowed_projects)
    with TestClient(app) as client:
        for candidate in (
            token[:-1] + ("x" if token[-1] != "x" else "y"),
            "not-a-token",
            "é.broken",
            "abc.abc",
        ):
            # Latin-1 cookie values travel through the raw header in the ASGI request.
            if not candidate.isascii():
                continue
            response = client.get("/workspace", headers={"Cookie": f"{COOKIE_NAME}={candidate}"})
            assert response.status_code == 401
        import agrix.access as access

        now = access.time.time()
        monkeypatch.setattr(access.time, "time", lambda: now + SESSION_SECONDS + 1)
        assert (
            client.get("/workspace", headers={"Cookie": f"{COOKIE_NAME}={token}"}).status_code
            == 401
        )


def test_password_errors_do_not_echo_credentials():
    app, _ = _app()
    with TestClient(app) as client:
        for payload in (
            {"password": {"secret": "private-value"}},
            {"password": "private-value", "extra": "private-value"},
            ["private-value"],
        ):
            response = client.post("/session", json=payload)
            assert response.status_code == 422
            assert "private-value" not in response.text
        response = client.post(
            "/session",
            content='{"password":"private-value"',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert "private-value" not in response.text
