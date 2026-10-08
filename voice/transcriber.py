import io
import wave
from typing import Protocol

from voice.audio import SAMPLE_WIDTH, TARGET_RATE


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
