import asyncio
import re
import time
import traceback

from agent.listener import RATE_LIMITED
from agent.trigger import fold_char
from voice import debug
from voice.audio import BYTES_PER_SECOND
from voice.transcriber import to_wav
from voice.window import CommandWindow, VoiceMessage

STT_TIMEOUT_S = 15.0
STT_FAILED = "No pude entender el audio, intenta de nuevo."
SEPARATORS = " ,.:;-!¡?¿"
GAP = r"[\s,.:;!?¡¿-]*"
WAKE_PHRASE = re.compile(rf"(?:\b(?:oye|oie|hey)\b{GAP})?\b(?:maca|maka|makka){GAP}(?:kino|quino|kin|quin)\b")


def strip_wake_phrase(text):
    folded = ""
    origin = []
    for index, char in enumerate(text):
        piece = fold_char(char)
        folded += piece
        origin.extend([index] * len(piece))
    dropped = set()
    for match in WAKE_PHRASE.finditer(folded):
        dropped.update(origin[match.start() : match.end()])
    kept = "".join(char for index, char in enumerate(text) if index not in dropped)
    remainder = " ".join(kept.split()).strip(SEPARATORS)
    return remainder if any(char.isalnum() for char in remainder) else ""


class VoiceCommandSession:
    def __init__(
        self,
        guild,
        text_channel,
        listener,
        transcriber,
        notifier,
        clock=time.monotonic,
        stt_timeout_s=STT_TIMEOUT_S,
        loop=None,
        **window_options,
    ):
        self._loop = loop
        self._warned = set()
        self._guild = guild
        self._text_channel = text_channel
        self._listener = listener
        self._transcriber = transcriber
        self._notifier = notifier
        self._stt_timeout_s = stt_timeout_s
        self._clock = clock
        self._member = None
        self._tasks = set()
        self.window = CommandWindow(self._announce, self._dispatch, clock=clock, **window_options)

    def trigger(self, member, tail_pcm=b""):
        if self.window.busy or self._listener.is_busy(self._guild.id):
            return False
        self._member = member
        return self.window.open(member.id, tail_pcm)

    async def join(self):
        while self._tasks:
            done, _ = await asyncio.wait(set(self._tasks))
            self._tasks.difference_update(done)

    def _spawn(self, coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None:
                coroutine.close()
                raise
            self._loop.call_soon_threadsafe(self._start, coroutine)
            return
        self._start(coroutine)

    def _start(self, coroutine):
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _announce(self, user_id):
        self._spawn(self._say(f"Te escucho, {self._member.display_name}."))

    def _dispatch(self, user_id, pcm):
        self._spawn(self._run(self._member, pcm))

    async def _say(self, text):
        try:
            await self._notifier.send(text)
        except Exception as error:
            print(f"[voz] notice error: {type(error).__name__}")

    async def _run(self, member, pcm):
        try:
            await self._transcribe_and_handle(member, pcm)
        except Exception:
            traceback.print_exc()
        finally:
            self.window.reset()

    async def _transcribe_and_handle(self, member, pcm):
        if self._listener.is_limited(member.id):
            if member.id not in self._warned:
                self._warned.add(member.id)
                await self._say(RATE_LIMITED)
            return
        self._warned.discard(member.id)
        debug.emit(f"stt audio={len(pcm) / BYTES_PER_SECOND:.2f}s")
        started = self._clock()
        try:
            transcript = await asyncio.wait_for(self._transcriber.transcribe(to_wav(pcm)), self._stt_timeout_s)
        except Exception as error:
            print(f"[voz] stt error: {type(error).__name__}")
            debug.emit(f"stt error {type(error).__name__}: {error}")
            await self._say(STT_FAILED)
            return
        request = strip_wake_phrase(transcript or "")
        debug.emit(f"stt latency={int((self._clock() - started) * 1000)}ms transcript={transcript!r} request={request!r}")
        if not request:
            return
        message = VoiceMessage(
            content=transcript,
            author=member,
            guild=self._guild,
            channel=self._text_channel,
        )
        await self._listener.handle_request(message, request)
