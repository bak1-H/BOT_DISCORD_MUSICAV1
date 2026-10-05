import json
import threading
from types import SimpleNamespace

import pytest

from agent import adapters
from agent.adapters import GeniusLyrics, GuildPlaylists, RunContextFactory
from agent.context import PendingAction
from agent.tools import execute_pending
from tests.agent_support import build_rig
from tests.discord_support import FakeIncomingMessage, FakeSender


def test_playlists_round_trip_through_the_guild_file(tmp_path):
    store = GuildPlaylists(str(tmp_path), 5)

    assert store.load() == {}
    store.save({"rock": [{"title": "A", "url": "u"}]})

    assert GuildPlaylists(str(tmp_path), 5).load() == {"rock": [{"title": "A", "url": "u"}]}
    assert json.loads((tmp_path / "5.json").read_text(encoding="utf-8")) == {"rock": [{"title": "A", "url": "u"}]}


def test_playlists_are_isolated_per_guild(tmp_path):
    GuildPlaylists(str(tmp_path), 1).save({"a": []})

    assert GuildPlaylists(str(tmp_path), 2).load() == {}


def test_playlists_read_the_legacy_utf8_format(tmp_path):
    (tmp_path / "9.json").write_text(json.dumps({"ñandú": [{"title": "Canción", "url": "u"}]}, ensure_ascii=False), encoding="utf-8")

    assert GuildPlaylists(str(tmp_path), 9).load()["ñandú"][0]["title"] == "Canción"


class FakeGenius:
    def __init__(self, song):
        self.song = song
        self.queries = []
        self.threads = []

    def search_song(self, title):
        self.queries.append(title)
        self.threads.append(threading.current_thread())
        return self.song


def patch_genius(monkeypatch, song):
    genius = FakeGenius(song)
    monkeypatch.setattr(adapters, "get_genius", lambda: genius)
    return genius


async def test_lyrics_search_runs_off_the_event_loop_thread(monkeypatch):
    genius = patch_genius(monkeypatch, SimpleNamespace(title="Tusa", artist="Karol G", lyrics="la la la"))

    result = await GeniusLyrics().search("tusa")

    assert genius.queries == ["tusa"]
    assert genius.threads[0] is not threading.main_thread()
    assert (result.title, result.artist, result.text) == ("Tusa", "Karol G", "la la la")


@pytest.mark.parametrize("song", [None, SimpleNamespace(title="T", artist="A", lyrics="")])
async def test_lyrics_search_returns_none_when_nothing_usable(monkeypatch, song):
    patch_genius(monkeypatch, song)

    assert await GeniusLyrics().search("tusa") is None


async def test_lyrics_search_propagates_provider_errors_for_the_tool_to_report(monkeypatch):
    class Failing:
        def search_song(self, title):
            raise RuntimeError("genius down")

    monkeypatch.setattr(adapters, "get_genius", lambda: Failing())

    with pytest.raises(RuntimeError):
        await GeniusLyrics().search("tusa")


def build_factory(tmp_path, rig, lol=None, lyrics=None):
    services = {}

    def music_for_guild(guild_id):
        services[guild_id] = services.get(guild_id, rig.service)
        return services[guild_id]

    return RunContextFactory(
        music_for_guild=music_for_guild,
        lol=lol or object(),
        lyrics=lyrics or object(),
        playlists_dir=lambda: str(tmp_path),
    )


async def test_factory_takes_ids_and_voice_channel_from_the_message(tmp_path):
    rig = build_rig(tmp_path)
    factory = build_factory(tmp_path, rig)
    author = FakeSender(user_id=42)
    message = FakeIncomingMessage("hola", author=author)

    ctx = factory(message)

    assert (ctx.guild_id, ctx.channel_id, ctx.author_id) == (1, message.channel.id, 42)
    assert ctx.voice_channel is author.voice.channel
    assert ctx.text_channel is message.channel
    assert ctx.music is rig.service


async def test_factory_reports_no_voice_channel_when_the_author_is_not_in_voice(tmp_path):
    rig = build_rig(tmp_path)
    factory = build_factory(tmp_path, rig)

    assert factory(FakeIncomingMessage("hola", author=FakeSender(in_voice=False))).voice_channel is None


async def test_factory_treats_a_voice_state_without_channel_as_not_in_voice(tmp_path):
    rig = build_rig(tmp_path)
    factory = build_factory(tmp_path, rig)
    author = FakeSender()
    author.voice = SimpleNamespace(channel=None)

    assert factory(FakeIncomingMessage("hola", author=author)).voice_channel is None


async def test_factory_wires_lol_lyrics_and_guild_playlists(tmp_path):
    rig = build_rig(tmp_path)
    lol, lyrics = object(), object()
    factory = build_factory(tmp_path, rig, lol=lol, lyrics=lyrics)

    ctx = factory(FakeIncomingMessage("hola"))

    assert ctx.lol is lol
    assert ctx.lyrics is lyrics
    ctx.playlists.save({"x": []})
    assert (tmp_path / "1.json").exists()


async def test_factory_resolves_the_playlists_dir_at_call_time(tmp_path):
    rig = build_rig(tmp_path)
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    current = {"dir": str(first)}
    factory = RunContextFactory(lambda guild_id: rig.service, object(), object(), lambda: current["dir"])

    current["dir"] = str(second)
    factory(FakeIncomingMessage("hola")).playlists.save({"x": []})

    assert (second / "1.json").exists()
    assert not (first / "1.json").exists()


async def test_each_message_gets_a_fresh_ledger(tmp_path):
    rig = build_rig(tmp_path)
    factory = build_factory(tmp_path, rig)

    first = factory(FakeIncomingMessage("hola"))
    first.ledger.tool_calls = 3

    assert factory(FakeIncomingMessage("hola")).ledger.tool_calls == 0


async def test_confirmed_playlist_delete_removes_the_real_file_entry(tmp_path):
    rig = build_rig(tmp_path)
    factory = build_factory(tmp_path, rig)
    ctx = factory(FakeIncomingMessage("hola"))
    ctx.playlists.save({"rock": [{"title": "A", "url": "u"}], "pop": []})

    message = await execute_pending(ctx, PendingAction("playlist_delete", "¿?", {"name": "rock"}))

    assert "rock" in message
    assert GuildPlaylists(str(tmp_path), 1).load() == {"pop": []}
