import json
import os
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from voice.audio import BYTES_PER_SECOND, TARGET_RATE

WAKE_PHRASES = ("oye maca kino", "oye maca quino")
UNKNOWN_TOKEN = "[unk]"
ANALYZED_SECONDS = 2.5
CHUNK_BYTES = BYTES_PER_SECOND // 4
LEADING_WORD = "oye"

TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class WakeHit:
    phrase: str
    end_seconds: float


class WakeWordDetector(Protocol):
    def detect(self, pcm_16k: bytes) -> WakeHit | None: ...


def phrases_from_env(env=None):
    source = os.environ if env is None else env
    raw = source.get("VOICE_WAKE_PHRASES", "")
    phrases = tuple(normalize_phrase(part) for part in raw.split(",") if part.strip())
    return phrases or WAKE_PHRASES


def normalize_phrase(phrase):
    return unicodedata.normalize("NFC", phrase.strip().lower())


def require_oye_from_env(env=None):
    source = os.environ if env is None else env
    value = source.get("VOICE_REQUIRE_OYE", "false").strip().lower()
    return value in TRUE_VALUES


def without_leading_word(phrase):
    words = phrase.split()
    return " ".join(words[1:]) if words and words[0] == LEADING_WORD else phrase


class VoskWakeDetector:
    def __init__(self, recognizer_factory, phrases=WAKE_PHRASES, require_oye=True, analyzed_seconds=ANALYZED_SECONDS):
        self.recognizer_factory = recognizer_factory
        self.analyzed_bytes = int(analyzed_seconds * BYTES_PER_SECOND) // 2 * 2
        self.failed = False
        full = tuple(normalize_phrase(phrase) for phrase in phrases)
        bare = tuple(without_leading_word(phrase) for phrase in full)
        grammar_phrases = tuple(dict.fromkeys(full + bare))
        accepted = full if require_oye else grammar_phrases
        self.accepted = tuple(phrase for phrase in accepted if len(phrase.split()) >= 2)
        self.grammar = json.dumps(list(grammar_phrases) + [UNKNOWN_TOKEN], ensure_ascii=False)

    def detect(self, pcm_16k):
        if self.failed:
            return None
        try:
            recognizer = self.recognizer_factory(self.grammar)
        except Exception as error:
            self.failed = True
            print(f"[voz] no se pudo cargar el reconocimiento de frase de activación: {error}")
            return None
        pcm = pcm_16k[: self.analyzed_bytes]
        for start in range(0, len(pcm), CHUNK_BYTES):
            if recognizer.AcceptWaveform(pcm[start : start + CHUNK_BYTES]):
                hit = self.match(recognizer.Result())
                if hit:
                    return hit
        return self.match(recognizer.FinalResult())

    def match(self, raw_result):
        words = json.loads(raw_result).get("result", [])
        spoken = [entry.get("word", "") for entry in words]
        for phrase in self.accepted:
            target = phrase.split()
            for start in range(len(spoken) - len(target) + 1):
                if spoken[start : start + len(target)] == target:
                    return WakeHit(phrase, float(words[start + len(target) - 1].get("end", 0.0)))
        return None


_model_lock = threading.Lock()
_models = {}


def load_vosk_model(model_dir):
    with _model_lock:
        if model_dir not in _models:
            from vosk import Model, SetLogLevel

            started = time.monotonic()
            SetLogLevel(-1)
            _models[model_dir] = Model(str(model_dir))
            print(f"[voz] modelo de reconocimiento cargado en {int((time.monotonic() - started) * 1000)} ms")
        return _models[model_dir]


def vosk_recognizer_factory(model_dir):
    def create(grammar):
        from vosk import KaldiRecognizer

        recognizer = KaldiRecognizer(load_vosk_model(model_dir), TARGET_RATE, grammar)
        recognizer.SetWords(True)
        return recognizer

    return create


def create_vosk_detector(env=None):
    source = os.environ if env is None else env
    model_dir = source.get("VOICE_VOSK_MODEL_DIR", "").strip()
    if not model_dir:
        return None
    if not Path(model_dir).is_dir():
        raise ValueError(f"La carpeta del modelo de voz no existe: {model_dir}")
    return VoskWakeDetector(
        vosk_recognizer_factory(model_dir),
        phrases=phrases_from_env(source),
        require_oye=require_oye_from_env(source),
    )
