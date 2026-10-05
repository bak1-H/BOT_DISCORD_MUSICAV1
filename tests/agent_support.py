import asyncio
from types import SimpleNamespace

from agent.context import LyricsResult, RunContext
from music.player import GuildPlayer
from music.service import MusicService
from tests.fakes import (
    FakeAudioSource,
    FakeExtractor,
    FakeVoiceClient,
    FakeVoiceGateway,
    RecordingNotifier,
)

GID = 1
CHANNEL_ID = 50
AUTHOR_ID = 7
VOICE_CHANNEL = object()


class FakePlaylists:
    def __init__(self, data=None):
        self.data = data if data is not None else {}
        self.saves = 0

    def load(self):
        return {name: list(songs) for name, songs in self.data.items()}

    def save(self, data):
        self.saves += 1
        self.data = {name: list(songs) for name, songs in data.items()}


class FakeLyrics:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.queries = []

    async def search(self, title):
        self.queries.append(title)
        if self.error is not None:
            raise self.error
        return self.result


def lyrics_result(text="la la la"):
    return LyricsResult(title="Tusa", artist="Karol G", text=text)


def song_url(name):
    return f"https://www.youtube.com/watch?v={name}"


def build_rig(tmp_path, in_voice=True, playlists=None, lyrics=None, connected=True):
    player = GuildPlayer(GID)
    client = None
    if connected:
        client = FakeVoiceClient()
    voice = FakeVoiceGateway(client)
    notifier = RecordingNotifier()
    extractor = FakeExtractor(str(tmp_path))
    service = MusicService(
        player=player,
        voice=voice,
        notifier=notifier,
        extractor=extractor,
        audio_source_factory=lambda path: FakeAudioSource(path, options="-vn"),
        loop=asyncio.get_running_loop(),
    )
    context = RunContext(
        guild_id=GID,
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        music=service,
        voice_channel=VOICE_CHANNEL if in_voice else None,
        text_channel=notifier,
        playlists=playlists,
        lyrics=lyrics,
    )
    return SimpleNamespace(
        service=service,
        player=player,
        voice=voice,
        notifier=notifier,
        extractor=extractor,
        ctx=context,
    )


def fill_queue(player, *names):
    return [player.enqueue(song_url(name), name) for name in names]


def queue_titles(player):
    return [track.title for track in player.queue]
