import asyncio
import time

from music.discord_adapters import ChannelNotifier
from voice.command import VoiceCommandSession
from voice.pipeline import ActivationSink, ListeningPipeline

PRIVACY_NOTICE = (
    "Estoy escuchando solo la palabra de activación; el audio de la orden se envía a un servicio externo "
    "únicamente después de decirla."
)
NOTICE_POLL_S = 0.5
NOTICE_ATTEMPTS = 20


class NoticeGatedTranscriber:
    def __init__(self, transcriber, ensure_notice):
        self._transcriber = transcriber
        self._ensure_notice = ensure_notice

    async def transcribe(self, wav_bytes):
        if not await self._ensure_notice():
            return ""
        return await self._transcriber.transcribe(wav_bytes)


class VoiceActivation:
    def __init__(
        self,
        detector,
        listener,
        transcriber,
        player_for,
        loop,
        rms_threshold,
        window_options=None,
        clock=time.monotonic,
        notice_poll_s=NOTICE_POLL_S,
    ):
        self._detector = detector
        self._listener = listener
        self._transcriber = transcriber
        self._player_for = player_for
        self._loop = loop
        self._rms_threshold = rms_threshold
        self._window_options = dict(window_options or {})
        self._clock = clock
        self._notice_poll_s = notice_poll_s
        self._pipelines = {}
        self._sessions = {}
        self._noticed = set()
        self._notice_sent = set()
        self._notice_locks = {}
        self._notice_tasks = set()
        self._logged = set()

    def ensure_listening(self, client):
        if not hasattr(client, "listen"):
            return False
        try:
            if not client.is_connected() or client.is_listening():
                return False
        except Exception as error:
            self._log_once(f"state {type(error).__name__}")
            return False
        guild = client.guild
        pipeline = self._build_pipeline(guild)
        try:
            client.listen(ActivationSink(pipeline), after=self._after_listening)
        except Exception as error:
            pipeline.stop()
            self._log_once(f"listen {type(error).__name__}")
            return False
        self.forget(guild.id)
        self._pipelines[guild.id] = pipeline
        pipeline.start()
        self._schedule_notice(client)
        return True

    def voice_state_changed(self, member, before, after):
        guild = member.guild
        if member.id == guild.me.id:
            if after.channel is None:
                self.forget(guild.id)
        elif before.channel != after.channel:
            pipeline = self._pipelines.get(guild.id)
            if pipeline is not None:
                pipeline.forget(member.id)
        client = guild.voice_client
        if client is not None:
            self.ensure_listening(client)

    def forget(self, guild_id):
        pipeline = self._pipelines.pop(guild_id, None)
        self._sessions.pop(guild_id, None)
        if pipeline is not None:
            pipeline.stop()

    def _build_pipeline(self, guild):
        return ListeningPipeline(
            self._detector,
            lambda: self._session_for(guild),
            rms_threshold=self._rms_threshold,
            clock=self._clock,
        )

    def _session_for(self, guild):
        player = self._player_for(guild.id)
        channel = player.text_channel
        if channel is None:
            return None
        gated = NoticeGatedTranscriber(self._transcriber, lambda: self._deliver_notice(guild.id))
        cached = self._sessions.get(guild.id)
        if cached is not None and (cached[0] is channel or cached[1].window.busy):
            return cached[1]
        session = VoiceCommandSession(
            guild,
            channel,
            self._listener,
            gated,
            ChannelNotifier(player),
            clock=self._clock,
            loop=self._loop,
            rms_threshold=self._rms_threshold,
            **self._window_options,
        )
        self._sessions[guild.id] = (channel, session)
        return session

    def _after_listening(self, error):
        if error is not None:
            self._log_once(f"after {type(error).__name__}")

    def _schedule_notice(self, client):
        key = (client.guild.id, client.channel.id)
        if key in self._noticed:
            return
        self._notice_sent.discard(client.guild.id)
        try:
            task = asyncio.get_running_loop().create_task(self._announce(key, client.guild.id))
        except RuntimeError:
            return
        self._noticed.add(key)
        self._notice_tasks.add(task)
        task.add_done_callback(self._notice_tasks.discard)

    async def _announce(self, key, guild_id):
        for _ in range(NOTICE_ATTEMPTS):
            if await self._deliver_notice(guild_id):
                return
            await asyncio.sleep(self._notice_poll_s)
        self._noticed.discard(key)

    async def _deliver_notice(self, guild_id):
        async with self._notice_locks.setdefault(guild_id, asyncio.Lock()):
            if guild_id in self._notice_sent:
                return True
            player = self._player_for(guild_id)
            channel = player.text_channel
            if channel is None:
                return False
            try:
                await channel.send(PRIVACY_NOTICE)
            except Exception as error:
                self._log_once(f"notice {type(error).__name__}")
                return False
            self._notice_sent.add(guild_id)
            return True

    def _log_once(self, kind):
        if kind not in self._logged:
            self._logged.add(kind)
            print(f"[voz] activación: {kind}")
