import audioop
import os
import time
from dataclasses import dataclass, field
from enum import Enum

from voice.audio import BYTES_PER_SECOND, DEFAULT_RMS_THRESHOLD, SAMPLE_WIDTH

SILENCE_SECONDS = 1.0
MAX_COMMAND_SECONDS = 8.0
NO_SPEECH_SECONDS = 4.0
MIN_TAIL_SECONDS = 0.6
ANALYSIS_FRAME_BYTES = BYTES_PER_SECOND // 50


class WindowState(Enum):
    IDLE = "idle"
    ARMED = "armed"
    CAPTURING = "capturing"
    SENT = "sent"


@dataclass(frozen=True)
class VoiceMessage:
    content: str
    author: object
    guild: object
    channel: object
    mentions: list = field(default_factory=list)
    reference: object = None

    async def reply(self, content=None, **kwargs):
        return await self.channel.send(content, **kwargs)


def positive_float_from_env(name, default, env=None):
    source = os.environ if env is None else env
    try:
        value = float(source.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def window_options_from_env(env=None):
    return {
        "silence_s": positive_float_from_env("VOICE_WINDOW_SILENCE_S", SILENCE_SECONDS, env),
        "max_s": positive_float_from_env("VOICE_WINDOW_MAX_S", MAX_COMMAND_SECONDS, env),
        "no_speech_s": positive_float_from_env("VOICE_WINDOW_NO_SPEECH_S", NO_SPEECH_SECONDS, env),
    }


def is_voiced(pcm, rms_threshold):
    pcm = pcm[: len(pcm) // SAMPLE_WIDTH * SAMPLE_WIDTH]
    return audioop.rms(pcm, SAMPLE_WIDTH) >= rms_threshold


def voiced_seconds(pcm, rms_threshold):
    voiced_bytes = sum(
        len(frame)
        for frame in (pcm[start : start + ANALYSIS_FRAME_BYTES] for start in range(0, len(pcm), ANALYSIS_FRAME_BYTES))
        if is_voiced(frame, rms_threshold)
    )
    return voiced_bytes / BYTES_PER_SECOND


def trailing_audio(segment_pcm, trigger_end_s):
    start = int(max(trigger_end_s, 0.0) * BYTES_PER_SECOND) // SAMPLE_WIDTH * SAMPLE_WIDTH
    return segment_pcm[start:]


class CommandWindow:
    def __init__(
        self,
        on_open,
        on_command,
        clock=time.monotonic,
        rms_threshold=DEFAULT_RMS_THRESHOLD,
        silence_s=SILENCE_SECONDS,
        max_s=MAX_COMMAND_SECONDS,
        no_speech_s=NO_SPEECH_SECONDS,
        min_tail_s=MIN_TAIL_SECONDS,
    ):
        self._on_open = on_open
        self._on_command = on_command
        self._clock = clock
        self._rms_threshold = rms_threshold
        self._silence_s = silence_s
        self._max_s = max_s
        self._no_speech_s = no_speech_s
        self._min_tail_s = min_tail_s
        self.state = WindowState.IDLE
        self.user_id = None
        self._buffer = bytearray()
        self._opened_at = 0.0
        self._speech_started_at = 0.0
        self._last_voice_at = 0.0

    @property
    def busy(self):
        return self.state is not WindowState.IDLE

    def open(self, user_id, tail_pcm=b""):
        if self.busy:
            return False
        self.user_id = user_id
        self._buffer = bytearray()
        tail_voiced_s = voiced_seconds(tail_pcm, self._rms_threshold)
        if tail_voiced_s >= self._min_tail_s:
            self.state = WindowState.SENT
            return self._guarded(self._on_command, user_id, bytes(tail_pcm))
        now = self._clock()
        self._opened_at = now
        if tail_voiced_s > 0:
            self.state = WindowState.CAPTURING
            self._buffer += tail_pcm
            self._speech_started_at = now
            self._last_voice_at = now
        else:
            self.state = WindowState.ARMED
        return self._guarded(self._on_open, user_id)

    def _guarded(self, callback, *args):
        try:
            callback(*args)
        except Exception as error:
            print(f"[voz] window callback error: {type(error).__name__}")
            self.reset()
            return False
        return True

    def feed(self, user_id, pcm):
        if user_id != self.user_id or self.state not in (WindowState.ARMED, WindowState.CAPTURING):
            return
        self.tick()
        if self.state not in (WindowState.ARMED, WindowState.CAPTURING):
            return
        voiced = is_voiced(pcm, self._rms_threshold)
        now = self._clock()
        if self.state is WindowState.ARMED:
            if not voiced:
                return
            self.state = WindowState.CAPTURING
            self._speech_started_at = now
        self._buffer += pcm
        if voiced:
            self._last_voice_at = now
        self.tick()

    def tick(self):
        now = self._clock()
        if self.state is WindowState.ARMED and now - self._opened_at >= self._no_speech_s:
            self.reset()
        elif self.state is WindowState.CAPTURING and (
            now - self._last_voice_at >= self._silence_s or now - self._speech_started_at >= self._max_s
        ):
            self.state = WindowState.SENT
            command = bytes(self._buffer)
            self._buffer = bytearray()
            self._guarded(self._on_command, self.user_id, command)

    def speaker_left(self, user_id):
        if user_id == self.user_id and self.state in (WindowState.ARMED, WindowState.CAPTURING):
            self.reset()

    def reset(self):
        self.state = WindowState.IDLE
        self.user_id = None
        self._buffer = bytearray()
