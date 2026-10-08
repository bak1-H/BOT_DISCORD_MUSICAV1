import sys
import threading

from voice.audio import BYTES_PER_SECOND
from voice.window import (
    CommandWindow,
    VoiceMessage,
    WindowState,
    trailing_audio,
    voiced_seconds,
    window_options_from_env,
)
from tests.voice_support import FRAME_BYTES, FRAME_SECONDS, FakeClock, silence, tone

SPEAKER = 7
OTHER = 8
THRESHOLD = 500


class Recorder:
    def __init__(self):
        self.opened = []
        self.commands = []

    def on_open(self, user_id):
        self.opened.append(user_id)

    def on_command(self, user_id, pcm):
        self.commands.append((user_id, pcm))


def build_window(**options):
    clock = FakeClock()
    recorder = Recorder()
    window = CommandWindow(recorder.on_open, recorder.on_command, clock=clock, rms_threshold=THRESHOLD, **options)
    return window, clock, recorder


def stream(window, clock, user_id, pcm):
    for start in range(0, len(pcm), FRAME_BYTES):
        clock.advance(FRAME_SECONDS)
        window.feed(user_id, pcm[start : start + FRAME_BYTES])


def test_trigger_opens_an_armed_window_and_announces_it_once():
    window, _, recorder = build_window()

    assert window.open(SPEAKER) is True

    assert window.state is WindowState.ARMED
    assert recorder.opened == [SPEAKER]


def test_trigger_while_a_window_is_open_is_ignored():
    window, _, recorder = build_window()
    window.open(SPEAKER)

    assert window.open(OTHER) is False

    assert window.user_id == SPEAKER
    assert recorder.opened == [SPEAKER]


def test_audio_from_other_users_is_ignored():
    window, clock, recorder = build_window()
    window.open(SPEAKER)

    stream(window, clock, OTHER, tone(1.0))
    clock.advance(1.0)
    window.tick()

    assert window.state is WindowState.ARMED
    assert recorder.commands == []


def test_silence_after_speech_closes_the_window_and_sends_the_audio():
    window, clock, recorder = build_window()
    window.open(SPEAKER)
    stream(window, clock, SPEAKER, tone(1.0))

    clock.advance(0.9)
    window.tick()
    assert recorder.commands == []

    clock.advance(0.2)
    window.tick()

    assert window.state is WindowState.SENT
    assert [user for user, _ in recorder.commands] == [SPEAKER]
    assert voiced_seconds(recorder.commands[0][1], THRESHOLD) >= 0.95


def test_hard_cap_closes_the_window_during_continuous_speech():
    window, clock, recorder = build_window()
    window.open(SPEAKER)

    stream(window, clock, SPEAKER, tone(12.0))

    assert len(recorder.commands) == 1
    assert len(recorder.commands[0][1]) <= 8.1 * BYTES_PER_SECOND
    assert window.state is WindowState.SENT


def test_no_speech_within_the_deadline_cancels_silently():
    window, clock, recorder = build_window()
    window.open(SPEAKER)
    stream(window, clock, SPEAKER, silence(3.0))

    clock.advance(1.1)
    window.tick()

    assert window.state is WindowState.IDLE
    assert recorder.commands == []
    assert window.open(OTHER) is True


def test_silent_frames_do_not_start_the_capture():
    window, clock, _ = build_window()
    window.open(SPEAKER)

    stream(window, clock, SPEAKER, silence(1.0))

    assert window.state is WindowState.ARMED


def test_tail_with_a_full_command_is_sent_immediately_without_announcing():
    window, _, recorder = build_window()
    tail = tone(0.8) + silence(0.8)

    assert window.open(SPEAKER, tail) is True

    assert recorder.opened == []
    assert recorder.commands == [(SPEAKER, tail)]
    assert window.state is WindowState.SENT


def test_short_voiced_tail_seeds_the_capture_instead_of_being_lost():
    window, clock, recorder = build_window()
    tail = tone(0.3) + silence(0.2)

    window.open(SPEAKER, tail)

    assert window.state is WindowState.CAPTURING
    assert recorder.opened == [SPEAKER]
    assert recorder.commands == []

    stream(window, clock, SPEAKER, tone(0.5))
    clock.advance(1.5)
    window.tick()

    assert recorder.commands[0][1].startswith(tail)
    assert voiced_seconds(recorder.commands[0][1], THRESHOLD) >= 0.75


def test_silent_tail_leaves_the_window_armed_and_empty():
    window, _, recorder = build_window()

    window.open(SPEAKER, silence(0.8))

    assert window.state is WindowState.ARMED
    assert recorder.opened == [SPEAKER]
    assert recorder.commands == []


def test_late_packet_after_the_no_speech_deadline_cancels_an_armed_window():
    window, clock, recorder = build_window()
    window.open(SPEAKER)

    clock.advance(60.0)
    window.feed(SPEAKER, tone(0.02))

    assert window.state is WindowState.IDLE
    assert recorder.commands == []
    assert window.open(OTHER) is True


def test_late_packet_after_a_long_gap_sends_the_first_command_without_the_late_audio():
    window, clock, recorder = build_window()
    window.open(SPEAKER)
    stream(window, clock, SPEAKER, tone(1.0))

    clock.advance(120.0)
    window.feed(SPEAKER, tone(0.02))

    assert window.state is WindowState.SENT
    assert len(recorder.commands) == 1
    assert len(recorder.commands[0][1]) == len(tone(1.0))


def test_failing_command_callback_returns_the_window_to_idle_and_a_later_trigger_works(capsys):
    clock = FakeClock()
    calls = []

    def on_command(user_id, pcm):
        calls.append(user_id)
        if len(calls) == 1:
            raise RuntimeError("boom")

    window = CommandWindow(lambda user_id: None, on_command, clock=clock, rms_threshold=THRESHOLD)

    assert window.open(SPEAKER, tone(0.8) + silence(0.8)) is False
    assert window.state is WindowState.IDLE
    assert window.busy is False
    assert "[voz]" in capsys.readouterr().out

    assert window.open(SPEAKER, tone(0.8) + silence(0.8)) is True
    assert window.state is WindowState.SENT


def test_failing_command_callback_on_silence_close_releases_the_window():
    clock = FakeClock()

    def on_command(user_id, pcm):
        raise RuntimeError("boom")

    window = CommandWindow(lambda user_id: None, on_command, clock=clock, rms_threshold=THRESHOLD)
    window.open(SPEAKER)
    stream(window, clock, SPEAKER, tone(1.0))
    clock.advance(1.5)
    window.tick()

    assert window.busy is False


def test_speaker_leaving_after_the_command_was_sent_keeps_the_window_sent():
    window, clock, recorder = build_window()
    window.open(SPEAKER)
    stream(window, clock, SPEAKER, tone(1.0))
    clock.advance(1.5)
    window.tick()

    window.speaker_left(SPEAKER)

    assert window.state is WindowState.SENT
    assert len(recorder.commands) == 1


def test_speaker_leaving_mid_window_releases_it_without_sending():
    window, clock, recorder = build_window()
    window.open(SPEAKER)
    stream(window, clock, SPEAKER, tone(0.5))

    window.speaker_left(SPEAKER)
    clock.advance(5.0)
    window.tick()

    assert window.state is WindowState.IDLE
    assert recorder.commands == []
    assert window.open(OTHER) is True


def test_another_user_leaving_does_not_close_the_window():
    window, _, _ = build_window()
    window.open(SPEAKER)

    window.speaker_left(OTHER)

    assert window.state is WindowState.ARMED


def test_sent_window_stays_busy_until_reset():
    window, clock, _ = build_window()
    window.open(SPEAKER)
    stream(window, clock, SPEAKER, tone(1.0))
    clock.advance(1.5)
    window.tick()

    assert window.open(OTHER) is False

    window.reset()

    assert window.open(OTHER) is True


def test_trailing_audio_skips_what_precedes_the_trigger_end():
    pcm = b"\x01\x00" * BYTES_PER_SECOND

    tail = trailing_audio(pcm, 0.5)

    assert len(tail) == len(pcm) - BYTES_PER_SECOND // 2


def test_window_options_read_the_environment_and_fall_back_on_bad_values():
    options = window_options_from_env(
        {"VOICE_WINDOW_SILENCE_S": "1.5", "VOICE_WINDOW_MAX_S": "abc", "VOICE_WINDOW_NO_SPEECH_S": "-2"}
    )

    assert options == {"silence_s": 1.5, "max_s": 8.0, "no_speech_s": 4.0}


async def test_voice_message_reply_goes_to_the_text_channel():
    sent = []

    class Channel:
        async def send(self, content=None, **kwargs):
            sent.append((content, kwargs))
            return "sent"

    message = VoiceMessage("pon algo", author=object(), guild=object(), channel=Channel())

    result = await message.reply("hola", view=None)

    assert result == "sent"
    assert sent == [("hola", {"view": None})]
    assert message.mentions == []
    assert message.reference is None


def test_reset_from_another_thread_waits_for_a_tick_in_progress():
    release = threading.Event()
    entered_clock = threading.Event()
    window, clock, _ = build_window()
    window.open(SPEAKER)

    def slow_clock():
        entered_clock.set()
        release.wait(timeout=5)
        return clock()

    window._clock = slow_clock
    ticker = threading.Thread(target=window.tick)
    resetter = threading.Thread(target=window.reset)
    ticker.start()
    assert entered_clock.wait(timeout=5)
    resetter.start()
    resetter.join(timeout=0.3)

    assert resetter.is_alive()
    release.set()
    ticker.join(timeout=5)
    resetter.join(timeout=5)

    assert not ticker.is_alive() and not resetter.is_alive()
    assert window.state is WindowState.IDLE


def test_feed_tick_and_reset_from_two_threads_never_raise_or_corrupt_the_state():
    window, clock, recorder = build_window(silence_s=0.05, no_speech_s=0.05)
    errors = []
    voiced = tone(FRAME_SECONDS)
    iterations = 400
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)

    def worker():
        try:
            for _ in range(iterations):
                window.open(SPEAKER)
                clock.advance(FRAME_SECONDS)
                window.feed(SPEAKER, voiced)
                window.tick()
        except Exception as error:
            errors.append(error)

    def loop():
        try:
            for _ in range(iterations):
                window.reset()
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=worker), threading.Thread(target=loop)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        sys.setswitchinterval(previous_interval)

    window.reset()

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert all(user_id == SPEAKER for user_id, _ in recorder.commands)
    assert window.state is WindowState.IDLE
    assert window.user_id is None
    assert window.busy is False
