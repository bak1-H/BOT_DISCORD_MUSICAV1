import asyncio
import traceback
from dataclasses import dataclass, field
from enum import Enum

import discord

from music import radio as radio_engine
from music.embeds import make_song_embed
from music.player import GuildPlayer, Track
from music.ports import ConnectResult, Extractor, Notifier, VoiceGateway
from music.ytdl import is_youtube_login_block, normalize_youtube_url

MAX_PLAYNEXT_FAILS = 3
ALONE_TIMEOUT = 180
IDLE_TIMEOUT = 900
LOOP_MODES = ["off", "song", "queue"]
MAX_QUEUE_SONGS_PER_CALL = 15


def _stop_playback(client) -> None:
    stop = getattr(client, "stop_playing", None) or client.stop
    stop()


@dataclass(frozen=True)
class EnqueueResult:
    track: Track
    preview: dict
    busy: bool


class RemoveStatus(Enum):
    REMOVED = "removed"
    OUT_OF_RANGE = "out_of_range"
    CHANGED = "changed"
    GONE = "gone"


@dataclass(frozen=True)
class RemoveResult:
    status: RemoveStatus
    track: Track | None = None


@dataclass
class QueueSongsResult:
    queued: list = field(default_factory=list)
    not_found: list = field(default_factory=list)
    too_long: list = field(default_factory=list)
    skipped_over_limit: int = 0


class MusicService:
    def __init__(
        self,
        player: GuildPlayer,
        voice: VoiceGateway,
        notifier: Notifier,
        extractor: Extractor,
        audio_source_factory,
        loop: asyncio.AbstractEventLoop,
        keep_alive=None,
        idle_timeout_s: float = IDLE_TIMEOUT,
        sleep=asyncio.sleep,
    ) -> None:
        self.player = player
        self.voice = voice
        self.notifier = notifier
        self.extractor = extractor
        self.audio_source_factory = audio_source_factory
        self.loop = loop
        self._keep_alive = keep_alive or (lambda: False)
        self._idle_timeout_s = idle_timeout_s
        self._sleep = sleep
        self._idle_task: asyncio.Task | None = None
        self._stopping = False

    async def connect(self, channel, text_channel) -> ConnectResult:
        async with self.player.voice_lock:
            client = self.voice.client
            if client and client.is_connected():
                result = ConnectResult.ALREADY_CONNECTED
            else:
                result = await self.voice.connect(channel)
                if result is not ConnectResult.CONNECTED:
                    return result
                self._arm_idle()
        self.player.text_channel = text_channel
        return result

    async def extract_info(self, query: str, is_search: bool = False, search_count: int = 1) -> dict:
        return {"entries": await self.extractor.search(query, search_count)}

    @staticmethod
    def _entry_url_and_title(video: dict) -> tuple[str, str]:
        url = normalize_youtube_url(video.get("webpage_url") or video.get("url"))
        return url, video.get("title", "Canción")

    def _is_busy(self) -> bool:
        client = self.voice.client
        return bool(client and (client.is_playing() or client.is_paused())) or self.player.playback_lock.locked()

    async def search_and_enqueue(self, query: str) -> EnqueueResult | None:
        entries = await self.extractor.search(query, 1)
        if not entries:
            return None

        video = entries[0]
        url, title = self._entry_url_and_title(video)

        track = self.player.enqueue(url, title)

        busy = self._is_busy()
        preview = {
            "title": title,
            "url": url,
            "thumbnail": video.get("thumbnail"),
            "duration": video.get("duration"),
            "uploader": video.get("uploader") or video.get("channel"),
        }
        return EnqueueResult(track=track, preview=preview, busy=busy)

    async def enqueue_next(self, query: str) -> EnqueueResult | None:
        entries = await self.extractor.search(query, 1)
        if not entries:
            return None

        video = entries[0]
        url, title = self._entry_url_and_title(video)

        busy = self._is_busy()
        track = self.player.enqueue_front(url, title)
        preview = {
            "title": title,
            "url": url,
            "thumbnail": video.get("thumbnail"),
            "duration": video.get("duration"),
            "uploader": video.get("uploader") or video.get("channel"),
        }
        await self.ensure_playing()
        return EnqueueResult(track=track, preview=preview, busy=busy)

    async def resolve_song(self, query: str) -> tuple[str, str] | None:
        entries = await self.extractor.search(query, 1)
        if not entries:
            return None
        return self._entry_url_and_title(entries[0])

    async def queue_songs(self, songs: list, max_duration_s: int | None = None) -> QueueSongsResult:
        result = QueueSongsResult()
        accepted = songs[:MAX_QUEUE_SONGS_PER_CALL]
        result.skipped_over_limit = len(songs) - len(accepted)

        for query in accepted:
            entries = await self.extractor.search(query, 1)
            if not entries:
                result.not_found.append(query)
                continue
            video = entries[0]
            url, title = self._entry_url_and_title(video)
            duration = video.get("duration")
            if max_duration_s is not None and duration and duration > max_duration_s:
                result.too_long.append(title)
                continue
            result.queued.append(self.player.enqueue(url, title))

        if result.queued:
            await self.ensure_playing()
        return result

    def remove(
        self,
        position: int,
        expected_entry_id: int | None = None,
        expected_title: str | None = None,
    ) -> RemoveResult:
        queue = self.player.queue
        if not isinstance(position, int) or position < 1 or position > len(queue):
            return RemoveResult(RemoveStatus.OUT_OF_RANGE)

        track = queue[position - 1]
        if expected_entry_id is not None and track.entry_id != expected_entry_id:
            return RemoveResult(RemoveStatus.CHANGED, track)
        if expected_title is not None and track.title != expected_title:
            return RemoveResult(RemoveStatus.CHANGED, track)

        del queue[position - 1]
        return RemoveResult(RemoveStatus.REMOVED, track)

    def remove_entry(self, entry_id: int) -> RemoveResult:
        queue = self.player.queue
        for index, track in enumerate(queue):
            if track.entry_id == entry_id:
                del queue[index]
                return RemoveResult(RemoveStatus.REMOVED, track)
        return RemoveResult(RemoveStatus.GONE)

    def clear_queue(self) -> int:
        removed = len(self.player.queue)
        self.player.queue = []
        return removed

    async def play_next(self) -> None:
        async with self.player.playback_lock:
            await self._play_next_locked()

    async def ensure_playing(self) -> None:
        async with self.player.playback_lock:
            client = self.voice.client
            if not client or not client.is_connected():
                return
            if client.is_playing() or client.is_paused():
                return
            await self._play_next_locked()

    def _on_song_end(self, error) -> None:
        asyncio.run_coroutine_threadsafe(self.play_next(), self.loop)

    async def radio_next(self) -> bool:
        return await radio_engine.radio_next(self.player, self.extract_info)

    def start_radio(self, query: str) -> None:
        self.player.radio.query = query
        self.player.radio.reset_pool()

    def stop_radio(self) -> None:
        self.player.radio.clear()

    async def _play_next_locked(self) -> None:
        player = self.player
        gid = player.guild_id

        mode = player.loop_mode
        prev = player.current
        if prev:
            if mode == "song":
                player.enqueue_front(prev["url"], prev["title"])
            elif mode == "queue":
                player.enqueue(prev["url"], prev["title"])

        queue = player.queue
        player.cleanup_audio_file()

        if not queue:
            if await self.radio_next():
                return await self._play_next_locked()
            player.current = None
            client = self.voice.client
            if client and self._keep_alive():
                if client.is_connected() and not self._stopping:
                    self._arm_idle()
            elif client:
                await client.disconnect()
            return

        track = queue.pop(0)
        queued_title = track.title
        url = normalize_youtube_url(track.url)

        try:
            info, audio_path, _ = await self.extractor.download(gid, url)

            song = {
                "title": info.get("title", queued_title),
                "url": url,
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader") or info.get("channel"),
            }
            player.current = song
            player.last_video_id = info.get("id")
            player.audio_file = audio_path

            async with player.voice_lock:
                client = self.voice.client
                if not client or not client.is_connected():
                    player.cleanup_audio_file()
                    return

                self._cancel_idle()
                source = self.audio_source_factory(audio_path)
                client.play(source, after=self._on_song_end)

            await self.notifier.send(embed=make_song_embed(song))
            player.fail_count = 0

        except Exception as e:
            if isinstance(e, discord.ClientException) and "already playing" in str(e).lower():
                return

            player.fail_count += 1
            traceback.print_exc()
            print(f"Play error: {e}")

            if player.fail_count == 1:
                await self.notifier.send(f"❌ Error al reproducir: {e}")

            if is_youtube_login_block(e):
                await self.notifier.send(f"⚠️ `{queued_title}` bloqueado por YouTube desde este servidor. Saltando.")
                player.fail_count = 0
                await self._play_next_locked()
                return

            if player.fail_count >= MAX_PLAYNEXT_FAILS:
                await self.notifier.send("❌ Falló la reproducción varias veces. Deteniendo y limpiando cola.")
                player.queue = []
                self._cancel_idle()
                async with player.voice_lock:
                    client = self.voice.client
                    if client and client.is_connected():
                        await client.disconnect()
                return

            await self._play_next_locked()

    def skip(self) -> bool:
        client = self.voice.client
        if client and client.is_playing():
            _stop_playback(client)
            return True
        return False

    def pause(self) -> bool:
        client = self.voice.client
        if client and client.is_playing():
            client.pause()
            return True
        return False

    def resume(self) -> bool:
        client = self.voice.client
        if client and client.is_paused():
            client.resume()
            return True
        return False

    async def stop(self) -> None:
        self._stopping = True
        try:
            self._cancel_idle()
            self.player.reset_session()
            async with self.player.voice_lock:
                client = self.voice.client
                if client:
                    _stop_playback(client)
                    if client.is_connected():
                        await client.disconnect()
        finally:
            self._stopping = False

    async def leave(self) -> bool:
        client = self.voice.client
        if not client or not client.is_connected():
            return False
        await self.stop()
        return True

    def _cancel_idle(self) -> None:
        task, self._idle_task = self._idle_task, None
        if task and not task.done():
            task.cancel()

    def _arm_idle(self) -> None:
        self._cancel_idle()
        if self._keep_alive():
            self._idle_task = asyncio.create_task(self._idle_timeout())

    async def _idle_timeout(self) -> None:
        await self._sleep(self._idle_timeout_s)
        self._idle_task = None
        client = self.voice.client
        if not client or not client.is_connected() or self._is_busy():
            return
        await self.stop()
        await self.notifier.send("👋 Me fui por inactividad.")

    def cycle_loop(self) -> str:
        current = self.player.loop_mode
        next_mode = LOOP_MODES[(LOOP_MODES.index(current) + 1) % len(LOOP_MODES)]
        self.player.loop_mode = next_mode
        return next_mode

    def refresh_alone_watch(self) -> None:
        client = self.voice.client
        if not client or not client.is_connected():
            return
        player = self.player
        humans = [m for m in client.channel.members if not m.bot]
        if not humans:
            if player.alone_task is None or player.alone_task.done():
                player.alone_task = asyncio.create_task(self.alone_timeout())
        else:
            task, player.alone_task = player.alone_task, None
            if task and not task.done():
                task.cancel()

    async def alone_timeout(self) -> None:
        await asyncio.sleep(ALONE_TIMEOUT)
        client = self.voice.client
        if not client or not client.is_connected():
            return
        if any(not m.bot for m in client.channel.members):
            return
        player = self.player
        self._cancel_idle()
        player.reset_session()
        player.alone_task = None
        async with player.voice_lock:
            client = self.voice.client
            if client and client.is_connected():
                await client.disconnect()
        await self.notifier.send("👋 Me fui porque quedé solo en el canal.")
