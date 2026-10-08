import json
import math
import struct

from voice.audio import BYTES_PER_SECOND

FRAME_SECONDS = 0.02
FRAME_BYTES = int(FRAME_SECONDS * BYTES_PER_SECOND)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def tone(seconds, amplitude=8000, rate=16000):
    samples = int(seconds * rate)
    return struct.pack(
        f"<{samples}h",
        *(int(amplitude * math.sin(2 * math.pi * 440 * index / rate)) for index in range(samples)),
    )


def silence(seconds, rate=16000):
    return bytes(int(seconds * rate) * 2)


def feed_frames(segmenter, clock, pcm):
    segments = []
    for start in range(0, len(pcm), FRAME_BYTES):
        clock.advance(FRAME_SECONDS)
        segment = segmenter.feed(pcm[start : start + FRAME_BYTES])
        if segment:
            segments.append(segment)
    return segments


class FakeRecognizer:
    def __init__(self, words=(), grammar=None, accepts=False):
        self.words = list(words)
        self.grammar = grammar
        self.accepts = accepts
        self.received = bytearray()
        self.final_calls = 0
        self.result_calls = 0
        self.words_enabled = None

    def SetWords(self, enabled):
        self.words_enabled = enabled

    def AcceptWaveform(self, data):
        self.received += data
        return self.accepts

    def Result(self):
        self.result_calls += 1
        return self.final_json()

    def FinalResult(self):
        self.final_calls += 1
        return self.final_json()

    def final_json(self):
        entries = [{"word": word, "start": index * 0.4, "end": index * 0.4 + 0.35} for index, word in enumerate(self.words)]
        return json.dumps({"text": " ".join(self.words), "result": entries})


class FakeRecognizerFactory:
    def __init__(self, words=(), accepts=False):
        self.words = words
        self.accepts = accepts
        self.created = []

    def __call__(self, grammar):
        recognizer = FakeRecognizer(self.words, grammar, self.accepts)
        self.created.append(recognizer)
        return recognizer
