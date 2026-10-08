import asyncio
import io
import math
import os
import threading
import time
import wave
from typing import Protocol

from voice import debug
from voice.audio import SAMPLE_WIDTH, TARGET_RATE

DEFAULT_STT_TIMEOUT_S = 20.0
TRANSCRIPTION_PROMPT = (
    "Transcribe literalmente el audio, que está en español de Chile. "
    "Los nombres propios de artistas, bandas y canciones (a menudo nombres en inglés dichos con acento español) "
    "escríbelos con su ortografía habitual, por ejemplo \"Bad Bunny\", \"Feid\" o \"Nightcore\", "
    "cuando el audio claramente se refiera a ellos. "
    "Devuelve solo el texto hablado, sin comentarios, comillas ni explicaciones. "
    "Si no hay voz, devuelve una cadena vacía."
)


class TranscriptionError(Exception):
    pass


def stt_timeout_from_env(env=None):
    source = os.environ if env is None else env
    try:
        value = float((source.get("VOICE_STT_TIMEOUT_S") or "").strip())
    except ValueError:
        return DEFAULT_STT_TIMEOUT_S
    return value if math.isfinite(value) and value > 0 else DEFAULT_STT_TIMEOUT_S


class Transcriber(Protocol):
    async def transcribe(self, wav_bytes: bytes) -> str: ...


def to_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(TARGET_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


class GeminiTranscriber:
    def __init__(self, client, model, timeout_s=DEFAULT_STT_TIMEOUT_S, client_factory=None):
        self._types = None
        self._client = client
        self._client_factory = client_factory
        self._model = model
        self._timeout_s = timeout_s
        self._build_lock = threading.Lock()
        self._ready = False

    def _ensure_ready(self):
        if self._ready:
            return
        with self._build_lock:
            if self._types is None:
                from google.genai import types

                self._types = types
            if self._client is None:
                self._client = self._client_factory()
            self._client.aio
            self._ready = True

    def warmup(self):
        started = time.monotonic()
        try:
            self._ensure_ready()
        except Exception as error:
            debug.emit(f"stt warmup failed {type(error).__name__}")
            return
        debug.emit(f"stt warmup {int((time.monotonic() - started) * 1000)}ms")

    async def transcribe(self, wav_bytes: bytes) -> str:
        try:
            if not self._ready:
                await asyncio.to_thread(self._ensure_ready)
            audio = self._types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(model=self._model, contents=[audio, TRANSCRIPTION_PROMPT]),
                self._timeout_s,
            )
            return (response.text or "").strip()
        except Exception as error:
            raise TranscriptionError(type(error).__name__) from None


def create_gemini_transcriber(env=None):
    source = os.environ if env is None else env
    api_key = (source.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY no está configurada")
    from agent.model import resolve_model_name

    model = (source.get("GEMINI_STT_MODEL") or "").strip() or resolve_model_name(source)
    timeout_s = stt_timeout_from_env(source)

    def build_client():
        from google import genai
        from google.genai import types

        return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))

    return GeminiTranscriber(None, model, timeout_s=timeout_s, client_factory=build_client)
