import audioop
import struct
import wave

import pytest

from tests.voice_support import silence, tone
from voice import evaluate


class LoudSegmentDetector:
    def detect(self, pcm_16k):
        return audioop.max(pcm_16k, 2) > 20000


def write_wav(path, pcm, rate=16000, channels=1):
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(pcm)


@pytest.fixture
def directories(tmp_path):
    positives = tmp_path / "pos"
    negatives = tmp_path / "neg"
    positives.mkdir()
    negatives.mkdir()
    return positives, negatives


def run(directories, *extra):
    positives, negatives = directories
    return evaluate.main([str(positives), str(negatives), *extra], detector=LoudSegmentDetector())


def test_go_when_thresholds_are_met(directories, capsys):
    positives, negatives = directories
    write_wav(positives / "a.wav", silence(0.5) + tone(1.0, 25000) + silence(1.5))
    write_wav(positives / "b.wav", tone(1.0, 25000) + silence(1.5))
    write_wav(negatives / "n.wav", silence(36) + tone(1.0, 25000) + silence(1.5))

    code = run(directories, "--max-false-positives-per-hour", "200")

    output = capsys.readouterr().out
    assert code == 0
    assert "positives: 2/2" in output
    assert "RESULT: GO" in output


def test_no_go_when_false_positives_exceed_the_limit(directories, capsys):
    positives, negatives = directories
    write_wav(positives / "a.wav", tone(1.0, 25000) + silence(1.5))
    write_wav(negatives / "n.wav", silence(36) + tone(1.0, 25000) + silence(1.5))

    code = run(directories)

    output = capsys.readouterr().out
    assert code == 1
    assert "false_positives_per_hour=" in output
    assert "RESULT: NO-GO" in output


def test_no_go_when_recall_is_below_the_minimum_and_threshold_is_a_parameter(directories):
    positives, negatives = directories
    write_wav(positives / "hit.wav", tone(1.0, 25000) + silence(1.5))
    write_wav(positives / "miss.wav", tone(1.0, 1500) + silence(1.5))
    write_wav(negatives / "n.wav", silence(36))

    assert run(directories) == 1
    assert run(directories, "--min-recall", "0.5") == 0


def test_empty_directories_are_rejected(directories, capsys):
    assert run(directories) == 2
    assert "at least one" in capsys.readouterr().out


def test_without_model_dir_the_real_detector_is_not_available(directories, monkeypatch, capsys):
    positives, negatives = directories
    write_wav(positives / "a.wav", tone(1.0))
    write_wav(negatives / "n.wav", tone(1.0))
    monkeypatch.delenv("VOICE_VOSK_MODEL_DIR", raising=False)

    code = evaluate.main([str(positives), str(negatives)])

    assert code == 2
    assert "VOICE_VOSK_MODEL_DIR" in capsys.readouterr().out


def test_zero_seconds_of_negative_audio_is_not_evidence(directories, capsys):
    positives, negatives = directories
    write_wav(positives / "a.wav", tone(1.0, 25000) + silence(1.5))
    write_wav(negatives / "n.wav", b"")

    assert run(directories) == 2
    assert "Sin evidencia" in capsys.readouterr().out


def test_zero_seconds_of_positive_audio_is_not_evidence(directories, capsys):
    positives, negatives = directories
    write_wav(positives / "a.wav", b"")
    write_wav(negatives / "n.wav", silence(36))

    assert run(directories) == 2
    assert "Sin evidencia" in capsys.readouterr().out


def test_float_wav_exits_with_code_two_naming_the_file(directories, capsys):
    positives, negatives = directories
    float_wav = positives / "float.wav"
    data = bytes(64)
    header = (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 3, 1, 16000, 64000, 4, 32)
        + b"data" + struct.pack("<I", len(data))
    )
    float_wav.write_bytes(header + data)
    write_wav(negatives / "n.wav", silence(1))

    assert run(directories) == 2
    assert "float.wav" in capsys.readouterr().out


def test_missing_model_dir_exits_with_code_two(directories, tmp_path, capsys):
    positives, negatives = directories
    write_wav(positives / "a.wav", tone(1.0))
    write_wav(negatives / "n.wav", tone(1.0))

    code = evaluate.main([str(positives), str(negatives), "--model-dir", str(tmp_path / "no-existe")])

    assert code == 2
    assert "no existe" in capsys.readouterr().out


def test_read_wav_converts_stereo_48k_to_mono_16k(tmp_path):
    path = tmp_path / "s.wav"
    write_wav(path, bytes(48000 * 4), rate=48000, channels=2)

    pcm = evaluate.read_wav_16k(path)

    assert abs(len(pcm) - 16000 * 2) <= 8


def test_unsupported_sample_width_is_rejected(tmp_path):
    path = tmp_path / "w.wav"
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(1)
        target.setframerate(16000)
        target.writeframes(bytes(100))

    with pytest.raises(ValueError):
        evaluate.read_wav_16k(path)
