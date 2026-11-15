import builtins

import pytest
from agrix.api import create_app
from agrix.config import Settings
from agrix.demo import demo_plot
from agrix.deployment import prepare_environment
from fastapi.testclient import TestClient

from speech_api import install_speech


def test_disabled_speech_never_imports_models_and_typed_chat_still_works(monkeypatch):
    monkeypatch.setenv("AGRIX_SPEECH_ENABLED", "false")
    app = create_app(
        "sqlite://",
        initialize=True,
        settings=Settings(access_password="test-password", session_secret="s" * 32),
    )
    original = builtins.__import__

    def guard(name, *args, **kwargs):
        if name.startswith(("agrix_voice", "torch", "transformers", "llama_cpp")):
            raise AssertionError("Text-only hosting must not import speech models")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    install_speech(app)
    with TestClient(app) as client:
        assert client.get("/speech/status").status_code == 401
        assert (
            client.post("/session", json={"password": "test-password"}).status_code
            == 200
        )
        status = client.get("/speech/status").json()
        assert status["enabled"] is False
        assert not status["recognition"]["available"]
        assert not status["synthesis"]["available"]
        assert not app.state.speech_tasks
        assert (
            client.post("/speech/transcribe", content=b"never processed").status_code
            == 503
        )
        assert (
            client.post(
                "/speech/synthesize", json={"text": "never processed"}
            ).status_code
            == 503
        )
        payload = demo_plot().model_dump(mode="json")
        payload["analysis_mode"] = "demo"
        plot = client.post("/plots", json=payload).json()
        analysis = client.post(
            f"/plots/{plot['id']}/analyses", json={"plot_revision": 1}
        )
        assert analysis.status_code == 200
        response = client.post(
            "/chat",
            json={
                "plot_id": plot["id"],
                "evaluation_ids": [],
                "message": "How much water is available?",
                "locale": "en",
            },
        )
        assert response.status_code == 200
        assert response.json()["message"]


def test_render_startup_requires_hosted_tls_database_and_disabled_speech(monkeypatch):
    values = {
        "AGRIX_DEPLOYMENT": "render",
        "AGRIX_ENV": "production",
        "DATABASE_URL": "postgresql://user:password@db.example/ghayth?sslmode=require",
        "AGRIX_ACCESS_PASSWORD": "test-password",
        "AGRIX_SESSION_SECRET": "s" * 32,
        "AGRIX_ANALYSIS_WORKERS": "1",
        "AGRIX_SPEECH_ENABLED": "false",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert prepare_environment().database_url.startswith("postgresql+psycopg://")
    monkeypatch.setenv("AGRIX_SPEECH_ENABLED", "true")
    with pytest.raises(ValueError, match="SPEECH_ENABLED=false"):
        prepare_environment()
    monkeypatch.setenv("AGRIX_SPEECH_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///temporary.sqlite3")
    with pytest.raises(ValueError, match="hosted PostgreSQL"):
        prepare_environment()
