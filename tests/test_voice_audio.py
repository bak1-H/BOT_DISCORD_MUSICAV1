import pytest

from tests.voice_support import FakeClock, feed_frames, silence, tone
from voice.audio import (
    BYTES_PER_SECOND,
    DEFAULT_RMS_THRESHOLD,
    Segmenter,
    rms_threshold_from_env,
    to_mono_16k,
)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def segmenter(clock):
    return Segmenter(clock=clock)


def test_to_mono_16k_converts_a_discord_frame():
    frame = bytes(3840)

    converted, state = to_mono_16k(frame)

    assert len(converted) == 640
    assert state is not None


def test_to_mono_16k_keeps_resampler_state_across_frames():
    first, state = to_mono_16k(bytes(3840))
    second, _ = to_mono_16k(bytes(3840), state)

    assert len(first) + len(second) == 1280


def test_to_mono_16k_drops_a_trailing_partial_frame():
    converted, _ = to_mono_16k(bytes(3841))
    empty, _ = to_mono_16k(b"")

    assert len(converted) == 640
    assert empty == b""


def test_feed_accepts_odd_length_buffers(segmenter):
    assert segmenter.feed(tone(0.02)[:-1]) is None
    assert segmenter.feed(b"") is None
    assert segmenter.feed(b"\x01") is None


def test_voice_after_a_long_gap_starts_a_new_segment(segmenter, clock):
    feed_frames(segmenter, clock, tone(1.0))
    clock.advance(2.0)

    closed = segmenter.feed(tone(0.02))

    assert closed is not None
    assert BYTES_PER_SECOND <= len(closed) <= BYTES_PER_SECOND * 1.1
    assert segmenter.active
    assert len(segmenter.buffer) < BYTES_PER_SECOND * 0.1


def test_silence_never_produces_a_segment(segmenter, clock):
    assert feed_frames(segmenter, clock, silence(3)) == []
    clock.advance(5)
    assert segmenter.tick() is None


def test_timer_closes_the_segment_when_packets_stop_arriving(segmenter, clock):
    assert feed_frames(segmenter, clock, tone(1.0)) == []

    clock.advance(0.5)
    assert segmenter.tick() is None
    clock.advance(0.4)
    segment = segmenter.tick()

    assert segment is not None
    assert len(segment) >= BYTES_PER_SECOND


def test_trailing_silent_packets_close_the_segment(segmenter, clock):
    segments = feed_frames(segmenter, clock, tone(1.0) + silence(1.5))

    assert len(segments) == 1
    assert BYTES_PER_SECOND <= len(segments[0]) <= BYTES_PER_SECOND * 2


def test_short_blip_is_discarded(segmenter, clock):
    feed_frames(segmenter, clock, tone(0.1))
    clock.advance(1)

    assert segmenter.tick() is None


def test_segment_is_capped_at_ten_seconds(segmenter, clock):
    segments = feed_frames(segmenter, clock, tone(10.5))

    assert len(segments) == 1
    assert len(segments[0]) >= 10 * BYTES_PER_SECOND
    assert len(segments[0]) < 10.1 * BYTES_PER_SECOND


def test_preroll_keeps_only_the_last_fraction_of_silence(segmenter, clock):
    feed_frames(segmenter, clock, silence(2.0) + tone(0.5))
    clock.advance(1)
    segment = segmenter.tick()

    expected = int(0.2 * BYTES_PER_SECOND) + int(0.5 * BYTES_PER_SECOND)
    assert segment is not None
    assert abs(len(segment) - expected) <= 640


def test_a_new_segment_starts_after_the_previous_one_closed(segmenter, clock):
    feed_frames(segmenter, clock, tone(0.6))
    clock.advance(1)
    assert segmenter.tick() is not None

    feed_frames(segmenter, clock, tone(0.6))
    clock.advance(1)

    assert segmenter.tick() is not None


def test_quiet_audio_below_threshold_is_ignored(clock):
    quiet = Segmenter(rms_threshold=2000, clock=clock)

    feed_frames(quiet, clock, tone(1.0, amplitude=500))
    clock.advance(1)

    assert quiet.tick() is None


@pytest.mark.parametrize(
    "env, expected",
    [({}, DEFAULT_RMS_THRESHOLD), ({"VOICE_VAD_RMS": "800"}, 800), ({"VOICE_VAD_RMS": "x"}, DEFAULT_RMS_THRESHOLD), ({"VOICE_VAD_RMS": "-3"}, DEFAULT_RMS_THRESHOLD)],
)
def test_rms_threshold_from_env(env, expected):
    assert rms_threshold_from_env(env) == expected
