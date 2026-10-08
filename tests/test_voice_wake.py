import json
import os
import sys
import threading
import types
from pathlib import Path

import pytest

from tests.voice_support import FakeClock, FakeRecognizer, FakeRecognizerFactory, feed_frames, silence, tone
from voice import wake
from voice.audio import BYTES_PER_SECOND, Segmenter
from voice.wake import (
    WAKE_PHRASES,
    VoskWakeDetector,
    WakeHit,
    create_vosk_detector,
    phrases_from_env,
    require_oye_from_env,
)


def detect(words, **options):
    factory = FakeRecognizerFactory(words)
    return VoskWakeDetector(factory, **options).detect(tone(1.0)), factory


def test_full_phrase_triggers_with_the_end_of_the_last_wake_word():
    hit, _ = detect(["oye", "maca", "kino", "[unk]", "[unk]"])

    assert hit == WakeHit("oye maca kino", pytest.approx(0.4 * 2 + 0.35))


def test_quino_variant_triggers():
    hit, _ = detect(["oye", "maca", "quino"])

    assert hit.phrase == "oye maca quino"


def test_phrase_spoken_after_other_words_still_triggers():
    hit, _ = detect(["[unk]", "oye", "maca", "kino"])

    assert hit is not None


@pytest.mark.parametrize(
    "words",
    [["maca"], ["quino"], ["kino"], ["oye"], ["oye", "maca"], ["oye", "kino"], ["maca", "[unk]", "kino"], ["[unk]", "[unk]"], []],
)
def test_single_words_and_unrelated_speech_never_trigger(words):
    hit, _ = detect(words, require_oye=False)

    assert hit is None


def test_bare_name_does_not_trigger_when_oye_is_required():
    hit, _ = detect(["maca", "quino"])

    assert hit is None


def test_bare_name_triggers_when_oye_is_not_required():
    hit, _ = detect(["maca", "quino"], require_oye=False)

    assert hit.phrase == "maca quino"


def test_grammar_always_lists_full_and_bare_variants_and_unknown_token():
    factory = FakeRecognizerFactory(["oye", "maca", "kino"])
    VoskWakeDetector(factory).detect(tone(1.0))

    grammar = json.loads(factory.created[0].grammar)

    assert set(grammar) == {"oye maca kino", "oye maca quino", "maca kino", "maca quino", "[unk]"}


def test_one_utterance_yields_one_hit_even_when_vosk_reports_it_twice():
    class DoubleReporting:
        def __init__(self, grammar):
            self.calls = 0

        def AcceptWaveform(self, data):
            return True

        def Result(self):
            return json.dumps({"result": [{"word": "oye", "end": 0.1}, {"word": "maca", "end": 0.3}, {"word": "kino", "end": 0.6}]})

        def FinalResult(self):
            return self.Result()

    hit = VoskWakeDetector(DoubleReporting).detect(tone(3.0))

    assert hit == WakeHit("oye maca kino", 0.6)


def test_only_the_first_two_and_a_half_seconds_are_analyzed():
    factory = FakeRecognizerFactory()

    VoskWakeDetector(factory).detect(tone(6.0))

    assert len(factory.created[0].received) == int(2.5 * BYTES_PER_SECOND)


def test_a_fresh_recognizer_is_created_for_every_segment():
    factory = FakeRecognizerFactory()
    detector = VoskWakeDetector(factory)

    detector.detect(tone(1.0))
    detector.detect(tone(1.0))

    assert len(factory.created) == 2


def test_vad_silence_never_reaches_the_recognizer():
    factory = FakeRecognizerFactory(["oye", "maca", "kino"])
    clock = FakeClock()
    segmenter = Segmenter(clock=clock)

    segments = feed_frames(segmenter, clock, silence(5))
    clock.advance(2)
    tail = segmenter.tick()

    assert segments == [] and tail is None
    assert factory.created == []


def test_single_word_phrases_in_config_are_dropped():
    detector = VoskWakeDetector(FakeRecognizerFactory(["maca"]), phrases=("maca", "oye"), require_oye=False)

    assert detector.accepted == ()


def test_phrases_from_env_default_and_override():
    assert phrases_from_env({}) == WAKE_PHRASES
    assert phrases_from_env({"VOICE_WAKE_PHRASES": " Oye Maca Kino , oye maca kiño "}) == ("oye maca kino", "oye maca kiño")


@pytest.mark.parametrize("env, expected", [({}, False), ({"VOICE_REQUIRE_OYE": ""}, False), ({"VOICE_REQUIRE_OYE": "true"}, True), ({"VOICE_REQUIRE_OYE": " ON "}, True), ({"VOICE_REQUIRE_OYE": "maybe"}, False)])
def test_require_oye_from_env(env, expected):
    assert require_oye_from_env(env) is expected


def test_phrases_are_normalized_to_nfc():
    assert phrases_from_env({"VOICE_WAKE_PHRASES": "oye maca kiño"}) == ("oye maca kiño",)
    assert VoskWakeDetector(FakeRecognizerFactory(), phrases=("oye maca kiño",)).accepted == ("oye maca kiño",)


def test_create_vosk_detector_needs_a_model_dir_and_does_not_load_it(monkeypatch, tmp_path):
    loaded = []
    monkeypatch.setattr(wake, "load_vosk_model", lambda model_dir: loaded.append(model_dir))
    monkeypatch.delitem(sys.modules, "vosk", raising=False)

    assert create_vosk_detector({}) is None
    detector = create_vosk_detector({"VOICE_VOSK_MODEL_DIR": str(tmp_path), "VOICE_REQUIRE_OYE": "false"})

    assert isinstance(detector, VoskWakeDetector)
    assert loaded == []
    assert "vosk" not in sys.modules
    assert "maca quino" in detector.accepted


def test_create_vosk_detector_rejects_a_missing_model_dir(tmp_path):
    missing = tmp_path / "no-existe"

    with pytest.raises(ValueError, match="no existe"):
        create_vosk_detector({"VOICE_VOSK_MODEL_DIR": str(missing)})


def test_a_failing_recognizer_factory_is_logged_once_and_then_yields_no_hit(capsys):
    calls = []

    def broken_factory(grammar):
        calls.append(grammar)
        raise ImportError("vosk")

    detector = VoskWakeDetector(broken_factory)

    assert detector.detect(tone(1.0)) is None
    assert detector.detect(tone(1.0)) is None
    assert len(calls) == 1
    assert capsys.readouterr().out.count("[voz]") == 1


def test_warmup_loads_the_recognizer_before_the_first_segment_and_failures_disable_the_detector(capsys):
    factory = FakeRecognizerFactory()
    detector = VoskWakeDetector(factory)

    detector.warmup()

    assert len(factory.created) == 1

    def broken(grammar):
        raise ImportError("vosk")

    failing = VoskWakeDetector(broken)
    failing.warmup()

    assert failing.failed is True
    assert failing.detect(tone(1.0)) is None
    assert capsys.readouterr().out.count("[voz]") == 1


def test_result_is_read_when_the_recognizer_accepts_a_waveform():
    factory = FakeRecognizerFactory(["oye", "maca", "kino"], accepts=True)

    hit = VoskWakeDetector(factory).detect(tone(1.0))

    assert hit is not None
    assert factory.created[0].result_calls >= 1


def test_vosk_recognizer_factory_builds_a_grammar_recognizer_with_word_timings(monkeypatch):
    calls = {}
    sentinel_model = object()

    class RecordingRecognizer(FakeRecognizer):
        def __init__(self, model, rate, grammar):
            super().__init__(grammar=grammar)
            calls["args"] = (model, rate, grammar)

    fake_vosk = types.ModuleType("vosk")
    fake_vosk.KaldiRecognizer = RecordingRecognizer
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk)
    monkeypatch.setattr(wake, "load_vosk_model", lambda model_dir: sentinel_model)

    recognizer = wake.vosk_recognizer_factory("dir")('["oye maca kino", "[unk]"]')

    assert calls["args"] == (sentinel_model, 16000, '["oye maca kino", "[unk]"]')
    assert isinstance(calls["args"][2], str)
    assert recognizer.words_enabled is True


def test_model_is_loaded_once_even_with_concurrent_first_use(monkeypatch):
    created = []
    started = threading.Event()
    release = threading.Event()

    class SlowModel:
        def __init__(self, path):
            started.set()
            release.wait(2)
            created.append(path)

    fake_vosk = types.ModuleType("vosk")
    fake_vosk.Model = SlowModel
    fake_vosk.SetLogLevel = lambda level: None
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk)
    monkeypatch.setattr(wake, "_models", {})
    results = []
    threads = [threading.Thread(target=lambda: results.append(wake.load_vosk_model("dir"))) for _ in range(3)]

    for thread in threads:
        thread.start()
    started.wait(2)
    release.set()
    for thread in threads:
        thread.join(2)

    assert created == ["dir"]
    assert len(results) == 3 and len({id(model) for model in results}) == 1


def test_real_model_detects_the_recorded_phrase():
    pytest.importorskip("vosk")
    model_dir = os.environ.get("VOICE_VOSK_MODEL_DIR", "")
    wav_path = os.environ.get("VOICE_TEST_WAV", "")
    if not (model_dir and Path(model_dir).is_dir() and wav_path and Path(wav_path).is_file()):
        pytest.skip("set VOICE_VOSK_MODEL_DIR and VOICE_TEST_WAV to run against the real model")
    from voice.evaluate import read_wav_16k, segments_of

    detector = create_vosk_detector({"VOICE_VOSK_MODEL_DIR": model_dir, "VOICE_REQUIRE_OYE": "false"})
    hits = [detector.detect(segment) for segment in segments_of(read_wav_16k(wav_path), 500)]

    assert any(hits)
