import asyncio
import os
from collections import deque

import discord


class FakeAudioSource:
    def __init__(self, path, **options):
        self.path = path
        self.options = options


class FakeVoiceClient:
    def __init__(self, channel=None):
        self.channel = channel
        self.connected = True
        self.playing = False
        self.paused = False
        self.after = None
        self.source = None
        self.play_calls = 0
        self.stop_calls = 0
        self.disconnect_calls = 0

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused

    def play(self, source, after=None):
        if self.playing or self.paused:
            raise discord.ClientException("Already playing audio.")
        self.play_calls += 1
        self.source = source
        self.after = after
        self.playing = True

    def stop(self):
        self.stop_calls += 1
        self.playing = False
        self.paused = False

    def pause(self):
        self.playing = False
        self.paused = True

    def resume(self):
        self.playing = True
        self.paused = False

    async def disconnect(self, force=False):
        self.disconnect_calls += 1
        self.connected = False
        self.playing = False

    def finish_song(self):
        self.playing = False
        after, self.after = self.after, None
        if after:
            after(None)


class RecordingNotifier:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, embed=None, **kwargs):
        self.messages.append({"content": content, "embed": embed, "kwargs": kwargs})

    @property
    def texts(self):
        return [m["content"] for m in self.messages if m["content"]]

    @property
    def embed_titles(self):
        return [m["embed"].title for m in self.messages if m["embed"] is not None]

    def has_text_containing(self, fragment):
        return any(fragment in text for text in self.texts)

    def has_embed_titled(self, fragment):
        return any(fragment in title for title in self.embed_titles if title)


class FakeMember:
    def __init__(self, is_bot=False):
        self.bot = is_bot


class FakeVoiceChannel:
    def __init__(self, ctx=None, members=None):
        self.ctx = ctx
        self.members = members if members is not None else [FakeMember()]
        self.connect_calls = 0
        self.connect_error = None

    async def connect(self, timeout=60):
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        voice_client = FakeVoiceClient(self)
        self.ctx.voice_client = voice_client
        return voice_client


class FakeVoiceState:
    def __init__(self, channel):
        self.channel = channel


class FakeAuthor:
    def __init__(self, voice=None):
        self.voice = voice


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id


class FakeContext:
    def __init__(self, guild_id=1, in_voice=True, connected=False):
        self.guild = FakeGuild(guild_id)
        self.notifier = RecordingNotifier()
        self.channel = self.notifier
        self.voice_client = None
        channel = FakeVoiceChannel(ctx=self)
        self.voice_channel = channel
        self.author = FakeAuthor(FakeVoiceState(channel) if in_voice else None)
        if connected:
            self.voice_client = FakeVoiceClient(channel)

    async def send(self, content=None, **kwargs):
        await self.notifier.send(content, **kwargs)


class FakeExtractor:
    def __init__(self, download_dir):
        self.download_dir = download_dir
        self.search_entries = []
        self.search_error = None
        self.search_calls = []
        self.download_outcomes = deque()
        self.download_calls = []

    @staticmethod
    def entry(video_id, title=None, duration=200, **extra):
        data = {
            "id": video_id,
            "title": title or f"Song {video_id}",
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            "duration": duration,
            "uploader": "Uploader",
            "thumbnail": f"https://img/{video_id}.jpg",
        }
        data.update(extra)
        return data

    async def ytdlp_extract(self, query, is_search=False, client="web", search_count=1):
        self.search_calls.append({"query": query, "search_count": search_count})
        if self.search_error is not None:
            raise self.search_error
        return {"entries": list(self.search_entries)[:search_count]}

    async def download_audio_with_fallback(self, gid, url):
        self.download_calls.append(url)
        outcome = self.download_outcomes.popleft() if self.download_outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        video_id = url.rsplit("=", 1)[-1]
        info = outcome or self.entry(video_id)
        path = os.path.join(self.download_dir, f"{gid}_{info['id']}.webm")
        with open(path, "wb") as f:
            f.write(b"audio")
        return info, path, "web"


async def settle(cycles=5):
    for _ in range(cycles):
        await asyncio.sleep(0)


def queued_pairs(bot_module, guild_id):
    return [(track.url, track.title) for track in bot_module.players.get(guild_id).queue]
