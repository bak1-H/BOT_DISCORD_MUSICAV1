from tests.fakes import FakeContext

GID = 1


class FakeSong:
    title = "Tusa"
    artist = "Karol G"
    lyrics = "la letra"


class FakeGenius:
    instances = []

    def __init__(self, token, **options):
        self.token = token
        self.options = options
        self.searches = []
        FakeGenius.instances.append(self)

    def search_song(self, title):
        self.searches.append(title)
        return FakeSong()


async def test_genius_client_is_created_lazily_once(isolated_bot, monkeypatch):
    FakeGenius.instances = []
    monkeypatch.setattr(isolated_bot, "_genius_client", None)
    monkeypatch.setattr(isolated_bot.lyricsgenius, "Genius", FakeGenius)
    monkeypatch.setenv("GENIUS_TOKEN", "token-from-env")

    assert FakeGenius.instances == []
    first = isolated_bot.get_genius()
    second = isolated_bot.get_genius()

    assert first is second
    assert len(FakeGenius.instances) == 1
    assert first.token == "token-from-env"
    assert first.options == {"skip_non_songs": True, "remove_section_headers": True}


async def test_lyrics_command_uses_the_lazy_client(isolated_bot, monkeypatch):
    FakeGenius.instances = []
    monkeypatch.setattr(isolated_bot, "_genius_client", None)
    monkeypatch.setattr(isolated_bot.lyricsgenius, "Genius", FakeGenius)
    ctx = FakeContext(guild_id=GID, connected=True)

    await isolated_bot.lyrics.callback(ctx, song="Tusa (Official Video)")

    assert ctx.notifier.has_text_containing("Tusa – Karol G")
    assert ctx.notifier.has_text_containing("la letra")
    assert len(FakeGenius.instances) == 1


async def test_lyrics_command_reports_error_when_client_cannot_be_created(isolated_bot, monkeypatch):
    def broken_genius(token, **options):
        raise TypeError("Invalid token")

    monkeypatch.setattr(isolated_bot, "_genius_client", None)
    monkeypatch.setattr(isolated_bot.lyricsgenius, "Genius", broken_genius)
    ctx = FakeContext(guild_id=GID, connected=True)

    await isolated_bot.lyrics.callback(ctx, song="Tusa")

    assert ctx.notifier.has_text_containing("Error al obtener la letra")
