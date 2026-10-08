import queue
import threading
from types import SimpleNamespace

from tests.voice_support import FakeClock, FakeDetector, stereo_tone, tone
from voice.pipeline import ActivationSink, ListeningPipeline
from voice.wake import WakeHit
from voice.window import CommandWindow, WindowState

FRAME_SECONDS = 0.02
STEREO_FRAME_BYTES = int(48000 * FRAME_SECONDS) * 4


def make_user(user_id=7, bot=False):
    return SimpleNamespace(id=user_id, display_name="Maxi", bot=bot)


class FakeSession:
    def __init__(self, clock):
        self.opened = []
        self.commands = []
        self.window = CommandWindow(
            lambda user_id: self.opened.append(user_id),
            lambda user_id, pcm: self.commands.append((user_id, pcm)),
            clock=clock,
            rms_threshold=500,
        )
        self.triggers = []

    def trigger(self, user, tail_pcm=b""):
        self.triggers.append((user, tail_pcm))
        return self.window.open(user.id, tail_pcm)


class Rig:
    def __init__(self, detector=None, queue_size=500, with_session=True):
        self.clock = FakeClock()
        self.detector = detector or FakeDetector()
        self.session = FakeSession(self.clock)
        self.available = with_session
        self.pipeline = ListeningPipeline(
            self.detector,
            lambda: self.session if self.available else None,
            rms_threshold=500,
            clock=self.clock,
            queue_size=queue_size,
        )
        self.user = make_user()

    def speak(self, seconds, user=None):
        pcm = stereo_tone(seconds)
        for start in range(0, len(pcm), STEREO_FRAME_BYTES):
            self.clock.advance(FRAME_SECONDS)
            self.pipeline.submit(user or self.user, pcm[start : start + STEREO_FRAME_BYTES])
            self.pipeline.step(timeout=0)

    def wait(self, seconds):
        self.clock.advance(seconds)
        self.pipeline.step(timeout=0)


def test_speech_without_a_hit_never_opens_the_window():
    rig = Rig()

    rig.speak(1.0)
    rig.wait(1.0)

    assert len(rig.detector.segments) == 1
    assert rig.session.triggers == []
    assert rig.session.window.state is WindowState.IDLE


def test_silence_alone_never_reaches_the_detector():
    rig = Rig()

    rig.wait(2.0)
    rig.pipeline.submit(rig.user, bytes(STEREO_FRAME_BYTES))
    rig.pipeline.step(timeout=0)

    assert rig.detector.segments == []


def test_the_tick_closes_a_segment_even_when_discord_stops_sending_packets():
    rig = Rig(FakeDetector([WakeHit("maca quino", 0.3)]))

    rig.speak(1.0)
    assert rig.detector.segments == []
    rig.wait(1.0)

    assert len(rig.detector.segments) == 1
    assert len(rig.session.triggers) == 1


def test_a_hit_with_a_long_tail_sends_the_command_right_away():
    rig = Rig(FakeDetector([WakeHit("maca quino", 0.3)]))

    rig.speak(1.0)
    rig.wait(1.0)

    assert rig.session.triggers[0][0] is rig.user
    assert len(rig.session.commands) == 1
    assert len(rig.session.commands[0][1]) > 0.6 * 32000


def test_a_hit_at_the_end_of_the_segment_arms_the_window_and_the_next_speech_is_the_command():
    rig = Rig(FakeDetector([WakeHit("maca quino", 1.0)]))

    rig.speak(1.0)
    rig.wait(1.0)
    assert rig.session.window.state is WindowState.ARMED
    assert rig.session.opened == [rig.user.id]

    rig.speak(1.0)
    rig.wait(1.5)

    assert len(rig.session.commands) == 1
    assert len(rig.detector.segments) == 1


def test_audio_from_other_users_is_ignored_while_the_window_is_open():
    rig = Rig(FakeDetector([WakeHit("maca quino", 1.0)]))
    rig.speak(1.0)
    rig.wait(1.0)
    other = make_user(8)

    rig.speak(1.0, user=other)
    rig.wait(1.0)

    assert rig.session.window.user_id == rig.user.id
    assert len(rig.detector.segments) == 1
    assert rig.session.commands == []


def test_a_departed_user_cancels_their_window_and_drops_their_state():
    rig = Rig(FakeDetector([WakeHit("maca quino", 1.0)]))
    rig.speak(1.0)
    rig.wait(1.0)

    rig.pipeline.forget(rig.user.id)
    rig.pipeline.step(timeout=0)

    assert rig.session.window.state is WindowState.IDLE
    assert rig.user.id not in rig.pipeline._tracks


def test_without_a_session_audio_is_ignored_and_nothing_breaks():
    rig = Rig(with_session=False)

    rig.speak(0.5)
    rig.wait(2.0)

    assert rig.detector.segments == []


def test_a_detector_failure_is_logged_once_and_the_loop_keeps_going(capsys):
    rig = Rig(FakeDetector(error=RuntimeError("boom")))

    rig.speak(1.0)
    rig.wait(1.0)
    rig.speak(1.0)
    rig.wait(1.0)

    assert len(rig.detector.segments) == 2
    assert capsys.readouterr().out.count("[voz] pipeline: RuntimeError") == 1


def test_undecodable_audio_is_logged_once_and_the_loop_keeps_going(capsys):
    rig = Rig()
    rig.pipeline.inbox.put((rig.user, object()))
    rig.pipeline.inbox.put((rig.user, object()))

    rig.pipeline.step(timeout=0)
    rig.pipeline.step(timeout=0)
    rig.speak(0.2)

    assert capsys.readouterr().out.count("[voz] pipeline: TypeError") == 1


def test_a_second_trigger_cannot_open_a_window_while_one_is_busy():
    rig = Rig(FakeDetector([WakeHit("maca quino", 1.0), WakeHit("maca quino", 1.0)]))
    rig.speak(1.0)
    rig.wait(1.0)

    rig.speak(0.5, user=make_user(9))
    rig.wait(1.0)

    assert len(rig.session.triggers) == 1


def test_the_worker_thread_starts_and_stops_with_the_pipeline():
    rig = Rig()
    rig.pipeline.start()
    thread = rig.pipeline._thread

    assert thread.is_alive()
    rig.pipeline.stop()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert rig.pipeline.stopped


class WeirdData:
    pcm = None


def test_sink_write_never_raises_on_odd_input():
    rig = Rig()
    sink = ActivationSink(rig.pipeline)
    payloads = [
        (rig.user, SimpleNamespace(pcm=tone(0.02))),
        (None, SimpleNamespace(pcm=tone(0.02))),
        (rig.user, SimpleNamespace(pcm=b"")),
        (rig.user, SimpleNamespace(pcm=None)),
        (rig.user, SimpleNamespace(pcm="text")),
        (rig.user, SimpleNamespace(pcm=object())),
        (rig.user, None),
        (rig.user, WeirdData()),
        (make_user(bot=True), SimpleNamespace(pcm=tone(0.02))),
        (object(), SimpleNamespace(pcm=tone(0.02))),
    ]

    for user, data in payloads:
        sink.write(user, data)

    assert rig.pipeline.inbox.qsize() >= 1


def test_sink_write_never_raises_when_the_queue_is_full():
    rig = Rig(queue_size=2)
    sink = ActivationSink(rig.pipeline)

    for _ in range(10):
        sink.write(rig.user, SimpleNamespace(pcm=tone(0.02)))

    assert rig.pipeline.inbox.qsize() == 2
    assert rig.pipeline.dropped == 8


def test_sink_write_does_not_block_when_the_queue_is_full():
    rig = Rig(queue_size=1)
    rig.pipeline.inbox.put_nowait((rig.user, b"x"))
    errors = []

    def write():
        try:
            ActivationSink(rig.pipeline).write(rig.user, SimpleNamespace(pcm=tone(0.02)))
        except Exception as error:
            errors.append(error)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    writer.join(timeout=2)

    assert not writer.is_alive()
    assert errors == []
    assert rig.pipeline.inbox.qsize() == 1
    assert rig.pipeline.dropped == 1


class WarmableDetector(FakeDetector):
    def __init__(self):
        super().__init__()
        self.warmups = []
        self.warmed = threading.Event()

    def warmup(self):
        self.warmups.append(threading.get_ident())
        self.warmed.set()


def test_the_detector_is_warmed_up_once_on_the_worker_thread():
    detector = WarmableDetector()
    rig = Rig(detector)
    rig.pipeline.start()
    try:
        assert detector.warmed.wait(timeout=3)
    finally:
        rig.pipeline.stop()
        rig.pipeline._thread.join(timeout=3)

    assert len(detector.warmups) == 1
    assert detector.warmups[0] != threading.get_ident()
    assert detector.warmups[0] == rig.pipeline._thread.ident


def test_the_transcriber_is_warmed_up_once_on_the_worker_thread():
    transcriber = WarmableDetector()
    pipeline = ListeningPipeline(FakeDetector(), lambda: None, transcriber=transcriber)
    pipeline.start()
    try:
        assert transcriber.warmed.wait(timeout=3)
    finally:
        pipeline.stop()
        pipeline._thread.join(timeout=3)

    assert transcriber.warmups == [pipeline._thread.ident]
    assert transcriber.warmups[0] != threading.get_ident()


def test_a_failing_detector_warmup_does_not_skip_the_transcriber_warmup():
    class Exploding(WarmableDetector):
        def warmup(self):
            raise RuntimeError("boom")

    transcriber = WarmableDetector()
    pipeline = ListeningPipeline(Exploding(), lambda: None, transcriber=transcriber)
    pipeline.start()
    try:
        assert transcriber.warmed.wait(timeout=3)
    finally:
        pipeline.stop()
        pipeline._thread.join(timeout=3)


def test_a_failing_warmup_is_logged_and_does_not_kill_the_worker(capsys):
    class Exploding(WarmableDetector):
        def warmup(self):
            raise RuntimeError("boom")

    rig = Rig(Exploding())
    rig.pipeline.start()
    thread = rig.pipeline._thread
    rig.pipeline.stop()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert "[voz] pipeline: RuntimeError" in capsys.readouterr().out


def test_audio_buffered_while_the_window_was_busy_never_triggers_a_wake_phrase_afterwards():
    rig = Rig(FakeDetector([WakeHit("maca quino", 0.3)]))
    rig.speak(0.5, user=make_user(8))
    rig.session.window.open(rig.user.id)
    rig.wait(0.1)
    rig.clock.advance(1.5)

    rig.session.window.reset()
    rig.wait(0.1)
    rig.wait(2.0)

    assert rig.detector.segments == []
    assert rig.session.triggers == []


def test_sink_wants_pcm_and_cleanup_stops_the_worker():
    rig = Rig()
    sink = ActivationSink(rig.pipeline)

    assert sink.wants_opus() is False
    sink.cleanup()

    assert rig.pipeline.stopped


def test_wake_hit_prints_debug_details_only_when_enabled(monkeypatch, capsys):
    monkeypatch.setenv("VOICE_DEBUG", "1")
    rig = Rig(FakeDetector([WakeHit("oye maca quino", 0.3)]))

    rig.speak(1.0)
    rig.wait(1.0)

    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("[voz-debug]")]
    assert len(lines) == 1
    assert "phrase='oye maca quino'" in lines[0]
    assert "end=0.30s" in lines[0]
    assert "segment=" in lines[0] and "tail=" in lines[0]


def test_wake_hit_still_triggers_when_debug_output_fails(monkeypatch):
    monkeypatch.setenv("VOICE_DEBUG", "1")
    monkeypatch.setattr("voice.debug.print", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("broken pipe")), raising=False)
    rig = Rig(FakeDetector([WakeHit("oye maca quiño", 0.3)]))

    rig.speak(1.0)
    rig.wait(1.0)

    assert rig.session.triggers


def test_wake_hit_prints_nothing_when_debug_is_off(monkeypatch, capsys):
    monkeypatch.delenv("VOICE_DEBUG", raising=False)
    rig = Rig(FakeDetector([WakeHit("oye maca quino", 0.3)]))

    rig.speak(1.0)
    rig.wait(1.0)

    assert rig.session.triggers
    assert "[voz-debug]" not in capsys.readouterr().out
