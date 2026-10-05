import asyncio
import json
import os
from pathlib import Path

import discord
import pytest

from music import embeds, lyrics, playlists, radio, ytdl
from music.player import GuildPlayer
from tests.fakes import FakeExtractor

MUSIC_PACKAGE = Path(__file__).resolve().parent.parent / "music"


@pytest.mark.parametrize("seconds, expected", [
    (None, "?"),
    (0, "?"),
    (65, "1:05"),
    (3725, "1:02:05"),
])
def test_format_duration(seconds, expected):
    assert embeds.format_duration(seconds) == expected


def test_song_embed_for_current_song_has_details():
    song = {"title": "T", "uploader": "U", "duration": 65, "thumbnail": "https://img/t.jpg"}

    embed = embeds.make_song_embed(song)

    assert embed.title == "🎵 Reproduciendo ahora"
    assert embed.description == "**T**"
    assert embed.color == discord.Color.green()
    assert [(f.name, f.value) for f in embed.fields] == [("Canal", "U"), ("Duración", "1:05")]
    assert embed.thumbnail.url == "https://img/t.jpg"


def test_song_embed_for_queued_song_without_details():
    embed = embeds.make_song_embed({"title": "T"}, in_queue=True)

    assert embed.title == "✅ Añadido a la cola"
    assert embed.color == discord.Color.blue()
    assert list(embed.fields) == []


@pytest.mark.parametrize("raw, expected", [
    ("Bad Bunny - Tusa (Official Video) [HD]", "bad bunny - tusa"),
    ("Karol G ft. Nicki Minaj - Tusa", "karol g"),
    ("Song   Name!!  ", "song name"),
    ("", ""),
    (None, ""),
])
def test_clean_title_for_lyrics(raw, expected):
    assert lyrics.clean_title_for_lyrics(raw) == expected


@pytest.mark.parametrize("value, expected", [
    (None, None),
    ("", None),
    ("abc123", "https://www.youtube.com/watch?v=abc123"),
    ("https://youtu.be/abc123", "https://youtu.be/abc123"),
])
def test_normalize_youtube_url(value, expected):
    assert ytdl.normalize_youtube_url(value) == expected


@pytest.mark.parametrize("message, expected", [
    ("Sign in to confirm you're not a bot", True),
    ("ERROR: Login Required", True),
    ("HTTP Error 403: Forbidden", False),
])
def test_is_youtube_login_block(message, expected):
    assert ytdl.is_youtube_login_block(RuntimeError(message)) is expected


def test_search_options_use_injected_settings():
    settings = ytdl.YtdlpSettings(proxy="http://proxy", cookies_file="c.txt", po_token="PO", visitor_data="VD")

    opts = ytdl.build_ytdlp_opts(settings, is_search=True, client="ios", search_count=3)

    assert opts["proxy"] == "http://proxy"
    assert opts["cookiefile"] == "c.txt"
    assert opts["default_search"] == "ytsearch3"
    assert opts["extract_flat"] == "in_playlist"
    assert opts["extractor_args"] == {
        "youtube": {"player_client": ["ios"], "po_token": ["ios+PO"], "visitor_data": ["VD"]}
    }


def test_plain_options_omit_search_and_tokens():
    opts = ytdl.build_ytdlp_opts(ytdl.YtdlpSettings(), is_search=False)

    assert opts["extractor_args"] == {"youtube": {"player_client": ["web"]}}
    assert "default_search" not in opts
    assert opts["format"] == "bestaudio*/best*"
    assert opts["js_runtimes"] == {"node": {}}


def test_options_are_not_shared_between_calls():
    settings = ytdl.YtdlpSettings()
    first = ytdl.build_ytdlp_opts(settings, is_search=False)
    first["js_runtimes"]["node"]["mutated"] = True

    second = ytdl.build_ytdlp_opts(settings, is_search=False)

    assert second["js_runtimes"] == {"node": {}}


def test_download_options_write_into_injected_directory(tmp_path):
    opts = ytdl.build_download_opts(ytdl.YtdlpSettings(), str(tmp_path), 77, "android")

    assert opts["outtmpl"] == os.path.join(str(tmp_path), "77_%(id)s.%(ext)s")
    assert opts["format"].startswith("bestaudio[ext=webm]")
    assert opts["extractor_args"]["youtube"]["player_client"] == ["android"]


class FakeYoutubeDL:
    instances = []
    failing_clients = set()

    def __init__(self, opts):
        self.opts = opts
        self.client = opts["extractor_args"]["youtube"]["player_client"][0]
        FakeYoutubeDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download):
        if self.client in FakeYoutubeDL.failing_clients:
            raise RuntimeError(f"{self.client} failed")
        return {"id": "vid", "title": "Title", "entries": None}

    def prepare_filename(self, info):
        path = self.opts["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", "webm")
        with open(path, "wb") as f:
            f.write(b"audio")
        return path


@pytest.fixture
def fake_youtube_dl(monkeypatch):
    FakeYoutubeDL.instances = []
    FakeYoutubeDL.failing_clients = set()
    monkeypatch.setattr(ytdl.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


async def test_download_uses_first_client_and_injected_directory(fake_youtube_dl, tmp_path):
    info, path, client = await ytdl.download_audio_with_fallback(ytdl.YtdlpSettings(), str(tmp_path), 5, "https://u")

    assert client == "web"
    assert info["id"] == "vid"
    assert path == os.path.join(str(tmp_path), "5_vid.webm")
    assert os.path.exists(path)


async def test_download_falls_back_to_next_client(fake_youtube_dl, tmp_path):
    fake_youtube_dl.failing_clients = {"web", "android_vr"}

    _, path, client = await ytdl.download_audio_with_fallback(ytdl.YtdlpSettings(), str(tmp_path), 5, "https://u")

    assert client == "android"
    assert [i.client for i in fake_youtube_dl.instances] == ["web", "android_vr", "android"]
    assert path.startswith(str(tmp_path))


async def test_download_raises_last_error_when_every_client_fails(fake_youtube_dl, tmp_path):
    fake_youtube_dl.failing_clients = set(ytdl.YT_CLIENTS)

    with pytest.raises(RuntimeError, match="ios failed"):
        await ytdl.download_audio_with_fallback(ytdl.YtdlpSettings(), str(tmp_path), 5, "https://u")


def test_playable_candidate_filters_live_long_and_excluded():
    entry = FakeExtractor.entry("a", duration=100)

    assert radio.is_playable_candidate(entry, 600, set()) is True
    assert radio.is_playable_candidate(entry, 600, {"a"}) is False
    assert radio.is_playable_candidate(FakeExtractor.entry("b", duration=601), 600, set()) is False
    assert radio.is_playable_candidate(FakeExtractor.entry("c", live_status="is_live"), 600, set()) is False
    assert radio.is_playable_candidate(FakeExtractor.entry("d", duration=None), 600, set()) is True
    assert radio.is_playable_candidate({"title": "no id"}, 600, set()) is False


def test_enqueue_entry_prefers_webpage_url_and_defaults_title():
    player = GuildPlayer(1)

    title = radio.enqueue_entry(player, {"id": "a", "webpage_url": "https://w/a", "url": "https://u/a"})

    assert title == "Desconocido"
    assert [(t.url, t.title) for t in player.queue] == [("https://w/a", "Desconocido")]


def test_enqueue_entry_without_url_enqueues_nothing():
    player = GuildPlayer(1)

    assert radio.enqueue_entry(player, {"id": "a", "title": "x"}) is None
    assert player.queue == []


def test_radio_candidate_excludes_played_and_last_video():
    player = GuildPlayer(1)
    player.radio.played.add("p")
    player.last_video_id = "l"

    assert radio.is_radio_candidate(player, FakeExtractor.entry("p")) is False
    assert radio.is_radio_candidate(player, FakeExtractor.entry("l")) is False
    assert radio.is_radio_candidate(player, FakeExtractor.entry("n")) is True


def test_enqueue_radio_pick_records_history():
    player = GuildPlayer(1)

    assert radio.enqueue_radio_pick(player, FakeExtractor.entry("a", title="A")) is True
    assert "a" in player.radio.played
    assert list(player.radio.history) == ["A"]
    assert [t.title for t in player.queue] == ["A"]


async def test_search_entries_keeps_only_dict_entries_and_forwards_arguments():
    calls = []

    async def extract(query, is_search=False, client="web", search_count=1):
        calls.append((query, is_search, search_count))
        return {"entries": [{"id": "a"}, None, "junk", {"id": "b"}]}

    entries = await radio.search_entries(extract, "q", search_count=3)

    assert entries == [{"id": "a"}, {"id": "b"}]
    assert calls == [("q", True, 3)]


async def test_search_entries_handles_non_dict_result():
    async def extract(query, is_search=False, client="web", search_count=1):
        return None

    assert await radio.search_entries(extract, "q", search_count=1) == []


async def test_radio_next_without_query_does_nothing():
    async def extract(*args, **kwargs):
        raise AssertionError("must not search")

    assert await radio.radio_next(GuildPlayer(1), extract) is False


async def test_radio_next_falls_back_to_search_and_enqueues_candidate(monkeypatch):
    async def no_suggestions(query, recent, count):
        return []

    monkeypatch.setattr(radio.ai_dj, "suggest_songs", no_suggestions)
    fake = FakeExtractor("unused")
    fake.search_entries = [fake.entry("a", title="A")]
    player = GuildPlayer(1)
    player.radio.query = "reggaeton"

    assert await radio.radio_next(player, fake.ytdlp_extract) is True
    assert [t.title for t in player.queue] == ["A"]
    assert fake.search_calls == [{"query": "reggaeton", "search_count": 5}]


async def test_radio_next_resets_played_pool_when_everything_was_played(monkeypatch):
    async def no_suggestions(query, recent, count):
        return []

    monkeypatch.setattr(radio.ai_dj, "suggest_songs", no_suggestions)
    fake = FakeExtractor("unused")
    fake.search_entries = [fake.entry("a", title="A")]
    player = GuildPlayer(1)
    player.radio.query = "q"
    player.radio.played = {"a"}

    assert await radio.radio_next(player, fake.ytdlp_extract) is True
    assert [t.title for t in player.queue] == ["A"]


async def test_radio_next_returns_false_when_search_raises(monkeypatch):
    async def no_suggestions(query, recent, count):
        return []

    monkeypatch.setattr(radio.ai_dj, "suggest_songs", no_suggestions)
    fake = FakeExtractor("unused")
    fake.search_error = RuntimeError("boom")
    player = GuildPlayer(1)
    player.radio.query = "q"

    assert await radio.radio_next(player, fake.ytdlp_extract) is False
    assert player.queue == []


def test_playlist_path_is_named_after_guild_id_inside_given_directory(tmp_path):
    assert playlists.playlist_path(str(tmp_path), 42) == os.path.join(str(tmp_path), "42.json")


def test_load_playlists_reads_existing_file_in_legacy_format(tmp_path):
    stored = {"favoritas": [{"title": "Canción ñandú", "url": "https://www.youtube.com/watch?v=x"}]}
    (tmp_path / "7.json").write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")

    assert playlists.load_playlists(str(tmp_path), 7) == stored


def test_load_playlists_for_unknown_guild_is_empty(tmp_path):
    assert playlists.load_playlists(str(tmp_path), 999) == {}


def test_save_playlists_roundtrips_unicode_and_writes_readable_json(tmp_path):
    data = {"favoritas": [{"title": "Canción ñandú", "url": "u"}]}

    playlists.save_playlists(str(tmp_path), 3, data)

    assert playlists.load_playlists(str(tmp_path), 3) == data
    assert "ñandú" in (tmp_path / "3.json").read_text(encoding="utf-8")


def test_music_modules_never_derive_paths_from_their_own_location():
    modules = sorted(MUSIC_PACKAGE.glob("*.py"))

    assert {m.name for m in modules} >= {"player.py", "ytdl.py", "radio.py", "playlists.py", "lyrics.py", "embeds.py"}
    for module in modules:
        assert "__file__" not in module.read_text(encoding="utf-8"), module.name


def test_music_modules_do_not_import_bot():
    for module in MUSIC_PACKAGE.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        assert "import bot" not in source, module.name
        assert "from bot" not in source, module.name
