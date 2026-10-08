import asyncio
import io
import os
import wave
from typing import Protocol

from voice.audio import SAMPLE_WIDTH, TARGET_RATE

REQUEST_TIMEOUT_S = 15.0
TRANSCRIPTION_PROMPT = (
    "Transcribe literalmente el audio, que está en español de Chile. "
    "Devuelve solo el texto hablado, sin comentarios, comillas ni explicaciones. "
    "Si no hay voz, devuelve una cadena vacía."
)


class TranscriptionError(Exception):
    pass


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
    def __init__(self, client, model, timeout_s=REQUEST_TIMEOUT_S):
        from google.genai import types

        self._types = types
        self._client = client
        self._model = model
        self._timeout_s = timeout_s

    async def transcribe(self, wav_bytes: bytes) -> str:
        audio = self._types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")
        try:
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
    from google import genai
    from google.genai import types

    from agent.model import resolve_model_name

    model = (source.get("GEMINI_STT_MODEL") or "").strip() or resolve_model_name(source)
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(REQUEST_TIMEOUT_S * 1000)))
    return GeminiTranscriber(client, model)
