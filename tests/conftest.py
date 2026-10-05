import asyncio
import os

for _name, _value in {
    "GENIUS_TOKEN": "dummy-genius-token",
    "DISCORD_TOKEN": "dummy-discord-token",
    "YOUTUBE_COOKIES_B64": "",
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "",
    "RIOT_API_KEY": "",
    "YTDLP_PROXY": "",
}.items():
    os.environ[_name] = _value

import pytest

import bot as bot_module
from tests.fakes import FakeAudioSource, FakeContext, FakeExtractor

ORIGINAL_DOWNLOAD_DIR = bot_module.DOWNLOAD_DIR
ORIGINAL_PLAYLISTS_DIR = bot_module.PLAYLISTS_DIR

PER_GUILD_STATE = (
    "queues",
    "current_song",
    "radio_query",
    "radio_played",
    "radio_history",
    "radio_suggestions",
    "last_video_id",
    "playnext_fail_count",
    "voice_state_locks",
    "current_audio_file",
    "loop_mode",
    "alone_tasks",
    "last_text_channel",
    "playback_locks",
)


def reset_per_guild_state():
    for name in PER_GUILD_STATE:
        state = getattr(bot_module, name)
        if name == "alone_tasks":
            for task in state.values():
                task.cancel()
        state.clear()


@pytest.fixture(autouse=True)
async def isolated_bot(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads"
    playlists_dir = tmp_path / "playlists"
    download_dir.mkdir()
    playlists_dir.mkdir()
    monkeypatch.setattr(bot_module, "DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setattr(bot_module, "PLAYLISTS_DIR", str(playlists_dir))
    monkeypatch.setattr(bot_module.discord, "FFmpegPCMAudio", FakeAudioSource)
    bot_module.bot.loop = asyncio.get_running_loop()
    reset_per_guild_state()
    yield bot_module
    reset_per_guild_state()


@pytest.fixture
def patch_extractor(monkeypatch):
    extractor = FakeExtractor(bot_module.DOWNLOAD_DIR)
    monkeypatch.setattr(bot_module, "ytdlp_extract", extractor.ytdlp_extract)
    monkeypatch.setattr(bot_module, "download_audio_with_fallback", extractor.download_audio_with_fallback)
    return extractor


@pytest.fixture
def ctx():
    return FakeContext(guild_id=1, connected=True)


@pytest.fixture
def disconnected_ctx():
    return FakeContext(guild_id=1)
