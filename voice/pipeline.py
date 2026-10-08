import queue
import threading
import time

from discord.ext.voice_recv import AudioSink

from voice.audio import DEFAULT_RMS_THRESHOLD, Segmenter, to_mono_16k
from voice.window import trailing_audio

QUEUE_SIZE = 500
POLL_SECONDS = 0.2


class UserTrack:
    def __init__(self, user, rms_threshold, clock):
        self.user = user
        self.segmenter = Segmenter(rms_threshold, clock)
        self.resample_state = None


class ListeningPipeline:
    def __init__(self, detector, session_provider, rms_threshold=DEFAULT_RMS_THRESHOLD, clock=time.monotonic, queue_size=QUEUE_SIZE):
        self.inbox = queue.Queue(queue_size)
        self.dropped = 0
        self._detector = detector
        self._session_provider = session_provider
        self._rms_threshold = rms_threshold
        self._clock = clock
        self._tracks = {}
        self._departed = set()
        self._logged = set()
        self._window_was_busy = False
        self._stopped = threading.Event()
        self._thread = None

    def submit(self, user, pcm):
        if user is None or not pcm or getattr(user, "bot", False):
            return
        try:
            self.inbox.put_nowait((user, bytes(pcm)))
        except queue.Full:
            self.dropped += 1
            self.log_once("QueueFull")

    def forget(self, user_id):
        self._departed.add(user_id)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="voice-pipeline", daemon=True)
            self._thread.start()

    def stop(self):
        self._stopped.set()

    @property
    def stopped(self):
        return self._stopped.is_set()

    def _run(self):
        self._guarded(self._warmup)
        while not self._stopped.is_set():
            self.step()
        self._tracks.clear()

    def _warmup(self):
        warmup = getattr(self._detector, "warmup", None)
        if warmup is not None:
            warmup()

    def step(self, timeout=POLL_SECONDS):
        try:
            item = self.inbox.get(timeout=timeout)
        except queue.Empty:
            item = None
        self._guarded(self._drop_departed)
        if item is not None:
            self._guarded(self._consume, *item)
        self._guarded(self._tick)

    def _guarded(self, action, *args):
        try:
            action(*args)
        except Exception as error:
            self.log_once(type(error).__name__)

    def log_once(self, kind):
        if kind in self._logged:
            return
        self._logged.add(kind)
        try:
            print(f"[voz] pipeline: {kind}")
        except Exception:
            return

    def _drop_departed(self):
        while self._departed:
            user_id = self._departed.pop()
            self._tracks.pop(user_id, None)
            session = self._session_provider()
            if session is not None:
                session.window.speaker_left(user_id)

    def _consume(self, user, pcm):
        session = self._session_provider()
        if session is None:
            return
        track = self._tracks.get(user.id)
        if track is None:
            track = self._tracks[user.id] = UserTrack(user, self._rms_threshold, self._clock)
        mono, track.resample_state = to_mono_16k(pcm, track.resample_state)
        window = session.window
        if window.busy:
            if window.user_id == user.id:
                window.feed(user.id, mono)
            return
        segment = track.segmenter.feed(mono)
        if segment:
            self._on_segment(session, user, segment)

    def _tick(self):
        session = self._session_provider()
        if session is None:
            return
        session.window.tick()
        busy = session.window.busy
        if self._window_was_busy and not busy:
            self._close_segmenters()
        self._window_was_busy = busy
        if busy:
            return
        for track in list(self._tracks.values()):
            segment = track.segmenter.tick()
            if segment:
                self._on_segment(session, track.user, segment)

    def _close_segmenters(self):
        for track in self._tracks.values():
            track.segmenter.close()

    def _on_segment(self, session, user, segment):
        hit = self._detector.detect(segment)
        if hit is None:
            return
        self._close_segmenters()
        session.trigger(user, trailing_audio(segment, hit.end_seconds))


class ActivationSink(AudioSink):
    def __init__(self, pipeline):
        self._pipeline = pipeline
        super().__init__()

    def wants_opus(self):
        return False

    def write(self, user, data):
        try:
            self._pipeline.submit(user, data.pcm)
        except Exception as error:
            self._pipeline.log_once(f"sink {type(error).__name__}")

    def cleanup(self):
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is not None:
            pipeline.stop()
