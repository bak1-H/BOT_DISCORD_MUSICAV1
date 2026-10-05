from music import extractor as extractor_module
from music.extractor import YtdlpExtractor
from music.ytdl import YtdlpSettings

SETTINGS = YtdlpSettings(proxy="http://proxy")


async def test_search_requests_a_search_with_the_given_count_and_returns_entries(monkeypatch):
    calls = []

    async def fake_extract(settings, query, is_search, client, search_count):
        calls.append((settings, query, is_search, client, search_count))
        return {"entries": [{"id": "a"}, {"id": "b"}]}

    monkeypatch.setattr(extractor_module.ytdl, "ytdlp_extract", fake_extract)

    entries = await YtdlpExtractor(SETTINGS, "dl").search("tusa", 3)

    assert entries == [{"id": "a"}, {"id": "b"}]
    assert calls == [(SETTINGS, "tusa", True, "web", 3)]


async def test_search_returns_empty_list_when_there_are_no_entries(monkeypatch):
    async def fake_extract(settings, query, is_search, client, search_count):
        return {"entries": None}

    monkeypatch.setattr(extractor_module.ytdl, "ytdlp_extract", fake_extract)

    assert await YtdlpExtractor(SETTINGS, "dl").search("nada", 1) == []


async def test_search_tolerates_a_non_dict_result(monkeypatch):
    async def fake_extract(settings, query, is_search, client, search_count):
        return None

    monkeypatch.setattr(extractor_module.ytdl, "ytdlp_extract", fake_extract)

    assert await YtdlpExtractor(SETTINGS, "dl").search("nada", 1) == []


async def test_download_forwards_settings_directory_guild_and_url(monkeypatch):
    calls = []

    async def fake_download(settings, download_dir, guild_id, url):
        calls.append((settings, download_dir, guild_id, url))
        return {"id": "a"}, "path.webm", "web"

    monkeypatch.setattr(extractor_module.ytdl, "download_audio_with_fallback", fake_download)

    result = await YtdlpExtractor(SETTINGS, "dl").download(7, "https://u")

    assert result == ({"id": "a"}, "path.webm", "web")
    assert calls == [(SETTINGS, "dl", 7, "https://u")]
