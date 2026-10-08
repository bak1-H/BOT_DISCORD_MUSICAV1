import audioop
import os
import time

SOURCE_RATE = 48000
TARGET_RATE = 16000
SAMPLE_WIDTH = 2
BYTES_PER_SECOND = TARGET_RATE * SAMPLE_WIDTH
STEREO_FRAME_BYTES = SAMPLE_WIDTH * 2

DEFAULT_RMS_THRESHOLD = 500
END_SILENCE_SECONDS = 0.8
MIN_VOICE_SECONDS = 0.3
MAX_SEGMENT_SECONDS = 10.0
PREROLL_SECONDS = 0.2


def to_mono_16k(pcm, state=None):
    whole_frames = len(pcm) // STEREO_FRAME_BYTES * STEREO_FRAME_BYTES
    mono = audioop.tomono(pcm[:whole_frames], SAMPLE_WIDTH, 0.5, 0.5)
    return audioop.ratecv(mono, SAMPLE_WIDTH, 1, SOURCE_RATE, TARGET_RATE, state)


def rms_threshold_from_env(env=None):
    source = os.environ if env is None else env
    try:
        value = int(source.get("VOICE_VAD_RMS", DEFAULT_RMS_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_RMS_THRESHOLD
    return value if value > 0 else DEFAULT_RMS_THRESHOLD


class Segmenter:
    def __init__(self, rms_threshold=DEFAULT_RMS_THRESHOLD, clock=time.monotonic):
        self.rms_threshold = rms_threshold
        self.clock = clock
        self.preroll = b""
        self.buffer = bytearray()
        self.voiced_bytes = 0
        self.last_voice_at = 0.0
        self.active = False

    def feed(self, pcm):
        pcm = pcm[: len(pcm) // SAMPLE_WIDTH * SAMPLE_WIDTH]
        now = self.clock()
        voiced = audioop.rms(pcm, SAMPLE_WIDTH) >= self.rms_threshold
        finished = self.tick() if voiced else None
        if voiced:
            self.last_voice_at = now
            if not self.active:
                self.active = True
                self.buffer = bytearray(self.preroll)
                self.preroll = b""
            self.voiced_bytes += len(pcm)
        if self.active:
            self.buffer += pcm
            if len(self.buffer) >= MAX_SEGMENT_SECONDS * BYTES_PER_SECOND:
                return self.close()
            return finished or self.tick()
        self.preroll = (self.preroll + pcm)[-int(PREROLL_SECONDS * BYTES_PER_SECOND):]
        return None

    def tick(self):
        if self.active and self.clock() - self.last_voice_at >= END_SILENCE_SECONDS:
            return self.close()
        return None

    def close(self):
        segment = bytes(self.buffer) if self.voiced_bytes >= MIN_VOICE_SECONDS * BYTES_PER_SECOND else None
        self.buffer = bytearray()
        self.voiced_bytes = 0
        self.preroll = b""
        self.active = False
        return segment
