import asyncio
import traceback
from dataclasses import dataclass

import discord

from music import radio as radio_engine
from music.embeds import make_song_embed
from music.player import GuildPlayer, Track
from music.ports import ConnectResult, Extractor, Notifier, VoiceGateway
from music.ytdl import is_youtube_login_block, normalize_youtube_url

MAX_PLAYNEXT_FAILS = 3
ALONE_TIMEOUT = 180
LOOP_MODES = ["off", "song", "queue"]


@dataclass(frozen=True)
class EnqueueResult:
    track: Track
    preview: dict
    busy: bool


class MusicService:
    def __init__(
        self,
        player: GuildPlayer,
        voice: VoiceGateway,
        notifier: Notifier,
        extractor: Extractor,
        audio_source_factory,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.player = player
        self.voice = voice
        self.notifier = notifier
        self.extractor = extractor
        self.audio_source_factory = audio_source_factory
        self.loop = loop

    async def connect(self, channel, text_channel) -> ConnectResult:
        async with self.player.voice_lock:
            client = self.voice.client
            if client and client.is_connected():
                result = ConnectResult.ALREADY_CONNECTED
            else:
                result = await self.voice.connect(channel)
                if result is not ConnectResult.CONNECTED:
                    return result
        self.player.text_channel = text_channel
        return result

    async def extract_info(self, query: str, is_search: bool = False, search_count: int = 1) -> dict:
        return {"entries": await self.extractor.search(query, search_count)}

    async def search_and_enqueue(self, query: str) -> EnqueueResult | None:
        entries = await self.extractor.search(query, 1)
        if not entries:
            return None

        video = entries[0]
        url = normalize_youtube_url(video.get("webpage_url") or video.get("url"))
        title = video.get("title", "Canción")

        track = self.player.enqueue(url, title)

        client = self.voice.client
        busy = bool(client and (client.is_playing() or client.is_paused())) or self.player.playback_lock.locked()
        preview = {
            "title": title,
            "url": url,
            "thumbnail": video.get("thumbnail"),
            "duration": video.get("duration"),
            "uploader": video.get("uploader") or video.get("channel"),
        }
        return EnqueueResult(track=track, preview=preview, busy=busy)

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
            if client:
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
                async with player.voice_lock:
                    client = self.voice.client
                    if client and client.is_connected():
                        await client.disconnect()
                return

            await self._play_next_locked()

    def skip(self) -> bool:
        client = self.voice.client
        if client and client.is_playing():
            client.stop()
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
        self.player.reset_session()
        async with self.player.voice_lock:
            client = self.voice.client
            if client:
                client.stop()
                if client.is_connected():
                    await client.disconnect()

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
        player.reset_session()
        player.alone_task = None
        async with player.voice_lock:
            client = self.voice.client
            if client and client.is_connected():
                await client.disconnect()
        await self.notifier.send("👋 Me fui porque quedé solo en el canal.")
