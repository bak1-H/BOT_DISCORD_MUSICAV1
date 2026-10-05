import pytest

from lol.embeds import build_comparison_embed, build_summoner_embed
from lol.riot import RiotApi, RiotLookupError
from lol.service import LolService, compare_players
from tests.fakes import FakeContext
from tests.lol_support import (
    FakeResponse,
    FakeRiotApi,
    flex_entry,
    make_player,
    ok_routes,
    session_factory_for,
    solo_entry,
)

FAKER = make_player("Faker", "KR1", solo=solo_entry("CHALLENGER", "I", 900, 300, 200), flex=flex_entry())
RIVAL = make_player("Rival", "LAS", solo=solo_entry("GOLD", "II", 45, 20, 15))


@pytest.fixture
def riot(isolated_bot, monkeypatch):
    api = FakeRiotApi({"Faker#KR1": FAKER, "Rival#LAS": RIVAL})
    monkeypatch.setattr(isolated_bot, "lol_service", LolService(api))
    monkeypatch.setattr(isolated_bot, "RIOT_API_KEY", "RGAPI-test")
    return api


async def test_invocador_sends_the_summoner_embed(isolated_bot, riot):
    ctx = FakeContext()

    await isolated_bot.invocador.callback(ctx, nombre="Faker#KR1")

    assert ctx.notifier.has_embed_titled("Faker#KR1")
    assert riot.queries == ["Faker#KR1"]


async def test_invocador_reports_lookup_errors_as_text(isolated_bot, riot):
    ctx = FakeContext()

    await isolated_bot.invocador.callback(ctx, nombre="Nadie#0000")

    assert ctx.notifier.has_text_containing("no encontrado")
    assert ctx.notifier.embed_titles == []


async def test_invocador_requires_a_tag(isolated_bot, riot):
    ctx = FakeContext()

    await isolated_bot.invocador.callback(ctx, nombre="Faker")

    assert ctx.notifier.has_text_containing("tag")
    assert riot.queries == []


async def test_invocador_without_configured_key_does_not_query(isolated_bot, riot, monkeypatch):
    monkeypatch.setattr(isolated_bot, "RIOT_API_KEY", "")
    ctx = FakeContext()

    await isolated_bot.invocador.callback(ctx, nombre="Faker#KR1")

    assert ctx.notifier.has_text_containing("RIOT_API_KEY")
    assert riot.queries == []


async def test_vs_sends_the_comparison_embed(isolated_bot, riot):
    ctx = FakeContext()

    await isolated_bot.vs.callback(ctx, nombres="Faker#KR1 Rival#LAS")

    assert ctx.notifier.has_embed_titled("Faker#KR1")
    assert ctx.notifier.has_embed_titled("Rival#LAS")
    assert sorted(riot.queries) == ["Faker#KR1", "Rival#LAS"]


async def test_vs_requires_two_riot_ids(isolated_bot, riot):
    ctx = FakeContext()

    await isolated_bot.vs.callback(ctx, nombres="Faker#KR1")

    assert ctx.notifier.has_text_containing("dos Riot IDs")
    assert riot.queries == []


async def test_vs_reports_lookup_errors_as_text(isolated_bot, riot):
    ctx = FakeContext()

    await isolated_bot.vs.callback(ctx, nombres="Faker#KR1 Ghost#0000")

    assert ctx.notifier.has_text_containing("Ghost#0000")


async def test_vs_rate_limited_replies_with_error_and_no_verdict(isolated_bot, monkeypatch):
    routes = ok_routes()
    routes["/lol/league/v4/entries/by-puuid/"] = FakeResponse(429)
    session, factory = session_factory_for(routes)
    riot = RiotApi("RGAPI-test", "la2", "americas", session_factory=factory)
    monkeypatch.setattr(isolated_bot, "lol_service", LolService(riot))
    monkeypatch.setattr(isolated_bot, "RIOT_API_KEY", "RGAPI-test")
    ctx = FakeContext()

    await isolated_bot.vs.callback(ctx, nombres="Faker#KR1 Rival#LAS")

    assert ctx.notifier.has_text_containing("límite")
    assert ctx.notifier.embed_titles == []
    assert not ctx.notifier.has_text_containing("Empate")


def test_summoner_embed_shows_both_queues_and_level():
    embed = build_summoner_embed(FAKER, "la2")

    assert embed.title == "Faker#KR1"
    assert "312" in embed.description and "LA2" in embed.description
    assert [field.name for field in embed.fields] == ["🎯 Solo/Duo", "👥 Flex 5v5"]
    assert "Challenger" in embed.fields[0].value
    assert "Silver" in embed.fields[1].value


def test_unranked_summoner_embed_says_unranked():
    embed = build_summoner_embed(make_player("Nuevo", "LAS"), "la2")

    assert all("Sin clasificar" in field.value for field in embed.fields)


def test_comparison_embed_names_both_players_and_the_verdict():
    embed = build_comparison_embed(compare_players(FAKER, RIVAL))

    assert embed.title == "⚔️ Faker#KR1  vs  Rival#LAS"
    assert [field.name for field in embed.fields] == ["Faker#KR1", "Rival#LAS", "Veredicto"]
    assert "Faker#KR1" in embed.fields[2].value
    assert "superior" in embed.fields[2].value


def test_comparison_embed_of_a_tie_says_so():
    twin = make_player("Gemelo", "A1", solo=solo_entry())
    other = make_player("Otro", "B2", solo=solo_entry())

    embed = build_comparison_embed(compare_players(twin, other))

    assert "Empate" in embed.fields[2].value


def test_lookup_error_is_an_exception_with_a_user_message():
    assert str(RiotLookupError("hola")) == "hola"
