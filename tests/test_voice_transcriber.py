import io
import wave

from tests.voice_support import FakeTranscriber, tone
from voice.transcriber import Transcriber, to_wav


def test_to_wav_writes_a_16k_mono_16bit_container():
    pcm = tone(0.5)

    with wave.open(io.BytesIO(to_wav(pcm))) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.readframes(wav.getnframes()) == pcm


async def test_a_fake_transcriber_satisfies_the_port():
    transcriber: Transcriber = FakeTranscriber("hola")

    assert await transcriber.transcribe(b"wav") == "hola"
