import argparse
import audioop
import os
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

from voice.audio import (
    BYTES_PER_SECOND,
    END_SILENCE_SECONDS,
    SAMPLE_WIDTH,
    TARGET_RATE,
    Segmenter,
    rms_threshold_from_env,
)
from voice.wake import create_vosk_detector

FRAME_SECONDS = 0.02
DEFAULT_MIN_RECALL = 0.9
DEFAULT_MAX_FALSE_POSITIVES_PER_HOUR = 1.0


class StepClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@dataclass(frozen=True)
class Report:
    positive_files: int
    positive_hits: int
    negative_seconds: float
    negative_hits: int
    min_recall: float
    max_false_positives_per_hour: float
    positive_seconds: float

    @property
    def recall(self):
        return self.positive_hits / self.positive_files if self.positive_files else 0.0

    @property
    def false_positives_per_hour(self):
        return self.negative_hits * 3600 / self.negative_seconds if self.negative_seconds else 0.0

    @property
    def passed(self):
        return self.recall >= self.min_recall and self.false_positives_per_hour <= self.max_false_positives_per_hour


def read_wav_16k(path):
    try:
        with wave.open(str(path)) as source:
            channels = source.getnchannels()
            if source.getsampwidth() != SAMPLE_WIDTH or channels not in (1, 2):
                raise ValueError(f"Formato wav no soportado (se requiere PCM de 16 bits, mono o estéreo): {path}")
            rate = source.getframerate()
            pcm = source.readframes(source.getnframes())
    except (wave.Error, EOFError) as error:
        raise ValueError(f"No se pudo leer el archivo wav {path}: {error}") from error
    if channels == 2:
        pcm = audioop.tomono(pcm, SAMPLE_WIDTH, 0.5, 0.5)
    return audioop.ratecv(pcm, SAMPLE_WIDTH, 1, rate, TARGET_RATE, None)[0]


def segments_of(pcm, rms_threshold):
    clock = StepClock()
    segmenter = Segmenter(rms_threshold, clock)
    frame_bytes = int(FRAME_SECONDS * BYTES_PER_SECOND)
    segments = []
    for start in range(0, len(pcm), frame_bytes):
        clock.now += FRAME_SECONDS
        segment = segmenter.feed(pcm[start : start + frame_bytes])
        if segment:
            segments.append(segment)
    clock.now += END_SILENCE_SECONDS
    tail = segmenter.tick()
    if tail:
        segments.append(tail)
    return segments


def count_hits(detector, segments):
    return sum(1 for segment in segments if detector.detect(segment))


def evaluate(positive_files, negative_files, detector, rms_threshold, min_recall, max_false_positives_per_hour):
    positive_hits = 0
    positive_seconds = 0.0
    for path in positive_files:
        pcm = read_wav_16k(path)
        positive_seconds += len(pcm) / BYTES_PER_SECOND
        if count_hits(detector, segments_of(pcm, rms_threshold)):
            positive_hits += 1
    negative_seconds = 0.0
    negative_hits = 0
    for path in negative_files:
        pcm = read_wav_16k(path)
        negative_seconds += len(pcm) / BYTES_PER_SECOND
        negative_hits += count_hits(detector, segments_of(pcm, rms_threshold))
    return Report(
        len(positive_files),
        positive_hits,
        negative_seconds,
        negative_hits,
        min_recall,
        max_false_positives_per_hour,
        positive_seconds,
    )


def wav_files(directory):
    return sorted(Path(directory).glob("*.wav"))


def main(argv=None, detector=None):
    parser = argparse.ArgumentParser(prog="python -m voice.evaluate")
    parser.add_argument("positives")
    parser.add_argument("negatives")
    parser.add_argument("--min-recall", type=float, default=DEFAULT_MIN_RECALL)
    parser.add_argument("--max-false-positives-per-hour", type=float, default=DEFAULT_MAX_FALSE_POSITIVES_PER_HOUR)
    parser.add_argument("--model-dir")
    args = parser.parse_args(argv)
    positives = wav_files(args.positives)
    negatives = wav_files(args.negatives)
    if not positives or not negatives:
        print("Both directories need at least one .wav file.")
        return 2
    if detector is None:
        env = dict(os.environ)
        if args.model_dir:
            env["VOICE_VOSK_MODEL_DIR"] = args.model_dir
        try:
            detector = create_vosk_detector(env)
        except ValueError as error:
            print(error)
            return 2
        if detector is None:
            print("Set VOICE_VOSK_MODEL_DIR or pass --model-dir.")
            return 2
    try:
        report = evaluate(
            positives,
            negatives,
            detector,
            rms_threshold_from_env(),
            args.min_recall,
            args.max_false_positives_per_hour,
        )
    except ValueError as error:
        print(error)
        return 2
    if report.negative_seconds <= 0 or report.positive_seconds <= 0:
        print("Sin evidencia suficiente: los audios positivos y negativos deben tener duración mayor a 0 segundos.")
        return 2
    print(f"positives: {report.positive_hits}/{report.positive_files} recall={report.recall:.2%} (min {report.min_recall:.2%})")
    print(
        f"negatives: {report.negative_hits} hits in {report.negative_seconds:.0f}s "
        f"false_positives_per_hour={report.false_positives_per_hour:.2f} (max {report.max_false_positives_per_hour:.2f})"
    )
    print("RESULT: GO" if report.passed else "RESULT: NO-GO")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
