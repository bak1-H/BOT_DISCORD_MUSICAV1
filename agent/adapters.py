import asyncio

from agent.context import LyricsResult, RunContext
from music import playlists as playlist_store
from music.lyrics import get_genius


class GuildPlaylists:
    def __init__(self, playlists_dir: str, guild_id: int) -> None:
        self._playlists_dir = playlists_dir
        self._guild_id = guild_id

    def load(self) -> dict:
        return playlist_store.load_playlists(self._playlists_dir, self._guild_id)

    def save(self, data: dict) -> None:
        playlist_store.save_playlists(self._playlists_dir, self._guild_id, data)


class GeniusLyrics:
    async def search(self, title: str) -> LyricsResult | None:
        loop = asyncio.get_running_loop()
        song = await loop.run_in_executor(None, lambda: get_genius().search_song(title))
        if not song or not song.lyrics:
            return None
        return LyricsResult(title=song.title, artist=song.artist, text=song.lyrics)


class RunContextFactory:
    def __init__(self, music_for_guild, lol, lyrics, playlists_dir) -> None:
        self._music_for_guild = music_for_guild
        self._lol = lol
        self._lyrics = lyrics
        self._playlists_dir = playlists_dir

    def __call__(self, message) -> RunContext:
        guild_id = message.guild.id
        voice = message.author.voice
        return RunContext(
            guild_id=guild_id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            music=self._music_for_guild(guild_id),
            voice_channel=voice.channel if voice else None,
            text_channel=message.channel,
            playlists=GuildPlaylists(self._playlists_dir(), guild_id),
            lyrics=self._lyrics,
            lol=self._lol,
            by_voice=getattr(message, "by_voice", False) is True,
        )
