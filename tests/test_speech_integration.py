"""Transport/authorization tests use explicit injected models, never fabricated production transcripts."""

import io
import sys
import wave
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/speech"))
from speech_api import install_speech

from agrix.api import create_app
from agrix.config import Settings


class Recognition:
    backend = "whisper"
    model_id = "injected-test-model"
    model = None

    def transcribe(self, audio):
        assert len(audio) == 16000
        return "سول على الما"


class Synthesis:
    backend = "kandir"
    model_id = "injected-test-model"
    model = None

    def speak(self, text):
        assert text == "جواب"
        return recording()


def recording():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes((np.sin(np.arange(16000) * 0.1) * 5000).astype("<i2").tobytes())
    return stream.getvalue()


def client(settings=None):
    app = create_app("sqlite+pysqlite:///:memory:", initialize=True, settings=settings)
    install_speech(app, recognizer=Recognition(), synthesizer=Synthesis())
    return TestClient(app)


def test_wav_transcription_and_synthesis_contract():
    with client() as c:
        assert c.post(
            "/speech/transcribe", content=recording(), headers={"Content-Type": "audio/wav"}
        ).json() == {"text": "سول على الما"}
        response = c.post("/speech/synthesize", json={"text": "جواب"})
        assert response.status_code == 200 and response.content[:4] == b"RIFF"
        assert response.headers["cache-control"] == "no-store"


def test_invalid_recordings_and_limits():
    with client() as c:
        assert (
            c.post(
                "/speech/transcribe", content=b"not wav", headers={"Content-Type": "audio/wav"}
            ).status_code
            == 422
        )
        assert (
            c.post(
                "/speech/transcribe", content=b"x", headers={"Content-Type": "audio/webm"}
            ).status_code
            == 415
        )
        assert (
            c.post(
                "/speech/transcribe",
                content=b"x" * 5_000_001,
                headers={"Content-Type": "audio/wav"},
            ).status_code
            == 413
        )
        assert c.post("/speech/synthesize", json={"text": "x" * 801}).status_code == 422


def test_speech_inherits_workspace_authentication():
    settings = Settings(access_password="integration-password", session_secret="x" * 32)
    with client(settings) as c:
        assert c.get("/speech/status").status_code == 401
        assert c.post("/speech/synthesize", json={"text": "جواب"}).status_code == 401
        assert c.post("/session", json={"password": "integration-password"}).status_code == 200
        assert c.get("/speech/status").status_code == 200
        assert (
            c.get("/speech/status", headers={"X-Project-ID": "another-project"}).status_code == 403
        )
