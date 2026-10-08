import asyncio
import io
import wave
from types import SimpleNamespace

import pytest

from tests.voice_support import FakeTranscriber, tone
from voice.transcriber import (
    TRANSCRIPTION_PROMPT,
    GeminiTranscriber,
    Transcriber,
    TranscriptionError,
    create_gemini_transcriber,
    to_wav,
)


def test_to_wav_writes_a_16k_mono_16bit_container():
    pcm = tone(0.5)

    with wave.open(io.BytesIO(to_wav(pcm))) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.readframes(wav.getnframes()) == pcm


async def test_a_fake_transcriber_satisfies_the_port():
    transcriber: Transcriber = FakeTranscriber("hola")

    assert await transcriber.transcribe(b"wav") == "hola"


API_KEY = "AIza-secret-key-123"


class FakeModels:
    def __init__(self, text="  hola mundo  ", error=None, hang=False):
        self.text = text
        self.error = error
        self.hang = hang
        self.calls = []

    async def generate_content(self, *, model, contents):
        self.calls.append((model, contents))
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


def fake_client(models):
    return SimpleNamespace(aio=SimpleNamespace(models=models))


async def test_gemini_transcriber_sends_the_wav_and_returns_stripped_text():
    models = FakeModels()

    text = await GeminiTranscriber(fake_client(models), "stt-model").transcribe(b"RIFFwav")

    model, contents = models.calls[0]
    assert text == "hola mundo"
    assert model == "stt-model"
    assert contents[0].inline_data.data == b"RIFFwav"
    assert contents[0].inline_data.mime_type == "audio/wav"
    assert contents[1] == TRANSCRIPTION_PROMPT


@pytest.mark.parametrize("text", [None, "", "   "])
async def test_gemini_transcriber_returns_an_empty_string_when_there_is_no_speech(text):
    assert await GeminiTranscriber(fake_client(FakeModels(text=text)), "m").transcribe(b"wav") == ""


async def test_gemini_transcriber_maps_errors_without_leaking_the_key():
    models = FakeModels(error=RuntimeError(f"bad request key={API_KEY}"))

    with pytest.raises(TranscriptionError) as raised:
        await GeminiTranscriber(fake_client(models), "m").transcribe(b"wav")

    assert str(raised.value) == "RuntimeError"
    assert API_KEY not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


async def test_gemini_transcriber_times_out_with_a_transcription_error():
    transcriber = GeminiTranscriber(fake_client(FakeModels(hang=True)), "m", timeout_s=0.05)

    with pytest.raises(TranscriptionError, match="TimeoutError"):
        await transcriber.transcribe(b"wav")


def test_factory_requires_the_api_key():
    with pytest.raises(ValueError):
        create_gemini_transcriber({"GEMINI_API_KEY": "  "})


def test_factory_prefers_the_stt_model_over_the_agent_model():
    env = {"GEMINI_API_KEY": API_KEY, "GEMINI_STT_MODEL": "stt-only", "GEMINI_AGENT_MODEL": "agent-model"}

    assert create_gemini_transcriber(env)._model == "stt-only"


def test_factory_falls_back_to_the_agent_model():
    env = {"GEMINI_API_KEY": API_KEY, "GEMINI_AGENT_MODEL": "agent-model"}

    assert create_gemini_transcriber(env)._model == "agent-model"
