import asyncio
import io
import threading
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
    stt_timeout_from_env,
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


def test_transcription_prompt_stays_literal_and_asks_for_standard_artist_spelling():
    assert "Transcribe literalmente" in TRANSCRIPTION_PROMPT
    assert "español de Chile" in TRANSCRIPTION_PROMPT
    assert "artistas, bandas y canciones" in TRANSCRIPTION_PROMPT
    assert "ortografía habitual" in TRANSCRIPTION_PROMPT
    for name in ("Bad Bunny", "Feid", "Nightcore"):
        assert name in TRANSCRIPTION_PROMPT
    assert "sin comentarios" in TRANSCRIPTION_PROMPT
    assert "cadena vacía" in TRANSCRIPTION_PROMPT


async def test_gemini_transcriber_makes_a_single_request_with_the_prompt():
    models = FakeModels()

    await GeminiTranscriber(fake_client(models), "m").transcribe(b"wav")

    assert len(models.calls) == 1
    assert models.calls[0][1][1] == TRANSCRIPTION_PROMPT


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


def test_stt_timeout_env_parsing():
    assert stt_timeout_from_env({}) == 20.0
    assert stt_timeout_from_env({"VOICE_STT_TIMEOUT_S": " 30 "}) == 30.0
    assert stt_timeout_from_env({"VOICE_STT_TIMEOUT_S": "7.5"}) == 7.5
    for bad in ("abc", "", "0", "-3", "nan", "inf"):
        assert stt_timeout_from_env({"VOICE_STT_TIMEOUT_S": bad}) == 20.0


def test_factory_applies_the_configured_stt_timeout():
    env = {"GEMINI_API_KEY": API_KEY, "VOICE_STT_TIMEOUT_S": "33"}

    assert create_gemini_transcriber(env)._timeout_s == 33.0


class CountingFactory:
    def __init__(self, failures=0):
        self.failures = failures
        self.threads = []

    def __call__(self):
        self.threads.append(threading.get_ident())
        if self.failures:
            self.failures -= 1
            raise RuntimeError("no sdk")
        return fake_client(FakeModels())


async def test_warmup_builds_the_client_once_off_the_calling_thread():
    factory = CountingFactory()
    transcriber = GeminiTranscriber(None, "m", client_factory=factory)
    worker = threading.Thread(target=transcriber.warmup)
    worker.start()
    worker.join()

    assert await transcriber.transcribe(b"wav") == "hola mundo"
    transcriber.warmup()

    assert len(factory.threads) == 1
    assert factory.threads[0] == worker.ident
    assert factory.threads[0] != threading.get_ident()


async def test_a_failing_warmup_does_not_raise_and_the_first_transcribe_retries():
    factory = CountingFactory(failures=1)
    transcriber = GeminiTranscriber(None, "m", client_factory=factory)

    transcriber.warmup()
    text = await transcriber.transcribe(b"wav")

    assert text == "hola mundo"
    assert len(factory.threads) == 2


async def test_a_slow_warmup_never_blocks_the_event_loop():
    started = threading.Event()
    release = threading.Event()

    def slow_factory():
        started.set()
        release.wait(timeout=5)
        return fake_client(FakeModels())

    transcriber = GeminiTranscriber(None, "m", client_factory=slow_factory)
    warming = threading.Thread(target=transcriber.warmup)
    warming.start()
    assert started.wait(timeout=3)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticking = asyncio.ensure_future(ticker())
    pending = asyncio.ensure_future(transcriber.transcribe(b"wav"))
    await asyncio.sleep(0.2)
    ticks_while_blocked = ticks
    assert not pending.done()
    release.set()
    text = await asyncio.wait_for(pending, 3)
    ticking.cancel()
    warming.join(timeout=3)

    assert ticks_while_blocked >= 5
    assert text == "hola mundo"


async def test_a_failed_warmup_retries_off_the_event_loop_thread():
    factory = CountingFactory(failures=1)
    transcriber = GeminiTranscriber(None, "m", client_factory=factory)
    transcriber.warmup()

    await transcriber.transcribe(b"wav")

    assert len(factory.threads) == 2
    assert factory.threads[1] != threading.get_ident()


def test_warmup_does_not_issue_any_request():
    models = FakeModels()
    GeminiTranscriber(fake_client(models), "m").warmup()

    assert models.calls == []
