"""Bounded speech inference; farm facts and actions always use the core engine's /chat."""

import asyncio
import gc
import importlib.util
import logging
import os

from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

LOG = logging.getLogger(__name__)
MAX_BYTES = 5_000_000


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=800)


def install_speech(app, *, recognizer=None, synthesizer=None):
    from agrix_voice.audio import InvalidAudio, read_recording
    from agrix_voice.models import Recognizer, Synthesizer

    recognizer = recognizer or Recognizer()
    synthesizer = synthesizer or Synthesizer()
    semaphore = asyncio.Semaphore(1)
    pending = set()
    timeout = float(os.getenv("AGRIX_INFERENCE_TIMEOUT", "170"))

    def installed(*modules):
        return all(importlib.util.find_spec(module) is not None for module in modules)

    def asr_available():
        return (
            installed("torch", "transformers", "qwen_asr")
            if recognizer.backend == "qwen"
            else installed("torch", "transformers")
        )

    def tts_available():
        modules = {
            "kandir": ("torch", "outetts", "llama_cpp", "soundfile"),
            "hadra": ("torch", "f5_tts", "soundfile"),
            "speecht5": ("torch", "transformers", "soundfile"),
        }
        return installed(*modules.get(synthesizer.backend, ("neutts", "soundfile")))

    async def run(operation):
        if semaphore.locked():
            raise HTTPException(503, "Speech service is busy. Please try again.")
        await semaphore.acquire()
        task = asyncio.create_task(asyncio.to_thread(operation))
        pending.add(task)

        def done(job):
            pending.discard(job)
            semaphore.release()
            if not job.cancelled():
                job.exception()

        task.add_done_callback(done)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except InvalidAudio as exc:
            raise HTTPException(422, str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(
                504, "Speech model is still loading or processing. Please try again."
            ) from exc
        except Exception as exc:
            LOG.exception("Speech inference failed")
            raise HTTPException(
                503, "Speech model unavailable. You can type your question."
            ) from exc

    @app.get("/speech/status", tags=["speech"])
    def status():
        return {
            "recognition": {
                "available": asr_available(),
                "model": recognizer.model_id,
                "backend": recognizer.backend,
                "loaded": recognizer.model is not None,
            },
            "synthesis": {
                "available": tts_available(),
                "model": synthesizer.model_id,
                "backend": synthesizer.backend,
                "loaded": synthesizer.model is not None,
            },
            "max_seconds": 25,
            "lazy_loading": True,
            "audio_retained": False,
        }

    @app.post("/speech/transcribe", tags=["speech"])
    async def transcribe(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "audio/wav":
            raise HTTPException(415, "Expected 16-bit PCM audio/wav")
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "Record at most 25 seconds")
            payload.extend(chunk)

        def recognize():
            audio, _ = read_recording(bytes(payload))
            if os.getenv("GHAYTH_VOICE_KEEP_MODELS") != "1":
                synthesizer.model = None
                gc.collect()
            return recognizer.transcribe(audio)

        text = await run(recognize)
        if not text:
            raise HTTPException(422, "No speech recognized")
        return {"text": text}

    @app.post("/speech/synthesize", tags=["speech"])
    async def synthesize(body: SpeechRequest):
        if not body.text.strip():
            raise HTTPException(422, "Supply text")

        def speak():
            if os.getenv("GHAYTH_VOICE_KEEP_MODELS") != "1":
                recognizer.model = None
                gc.collect()
            return synthesizer.speak(body.text)

        return Response(
            await run(speak), media_type="audio/wav", headers={"Cache-Control": "no-store"}
        )
