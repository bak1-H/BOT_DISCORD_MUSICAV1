import pytest

from lol.riot import RiotLookupError
from lol.service import LolService, rank_summary
from tests.lol_support import FakeRiotApi, flex_entry, make_player, solo_entry


def build_service(**players):
    riot = FakeRiotApi({riot_id: player for riot_id, player in players.items()})
    return riot, LolService(riot)


async def test_summoner_returns_the_player_from_the_riot_api():
    faker = make_player(solo=solo_entry())
    riot, service = build_service(**{"Faker#KR1": faker})

    assert await service.summoner("Faker#KR1") is faker
    assert riot.queries == ["Faker#KR1"]


async def test_summoner_propagates_lookup_errors():
    riot, service = build_service(**{"Faker#KR1": RiotLookupError("API key inválida o expirada.")})

    with pytest.raises(RiotLookupError):
        await service.summoner("Faker#KR1")


async def test_compare_queries_both_players_and_picks_the_higher_rank():
    gold = make_player("Gold", "A1", solo=solo_entry("GOLD", "I", 10, 10, 10))
    silver = make_player("Silver", "B2", solo=solo_entry("SILVER", "I", 10, 10, 10))
    riot, service = build_service(**{"Gold#A1": gold, "Silver#B2": silver})

    comparison = await service.compare("Gold#A1", "Silver#B2")

    assert sorted(riot.queries) == ["Gold#A1", "Silver#B2"]
    assert comparison.winner is gold
    assert comparison.first is gold and comparison.second is silver
    assert comparison.points_first > comparison.points_second


async def test_compare_is_symmetric_when_the_second_player_is_better():
    gold = make_player("Gold", "A1", solo=solo_entry("GOLD", "I", 10, 10, 10))
    diamond = make_player("Diamond", "B2", solo=solo_entry("DIAMOND", "IV", 0, 10, 10))
    riot, service = build_service(**{"Gold#A1": gold, "Diamond#B2": diamond})

    comparison = await service.compare("Gold#A1", "Diamond#B2")

    assert comparison.winner is diamond


async def test_identical_players_tie():
    first = make_player("Uno", "A1", solo=solo_entry(), flex=flex_entry())
    second = make_player("Dos", "B2", solo=solo_entry(), flex=flex_entry())
    riot, service = build_service(**{"Uno#A1": first, "Dos#B2": second})

    comparison = await service.compare("Uno#A1", "Dos#B2")

    assert comparison.winner is None
    assert comparison.points_first == comparison.points_second


async def test_unranked_loses_against_ranked():
    ranked = make_player("Ranked", "A1", solo=solo_entry("IRON", "IV", 0, 1, 1))
    unranked = make_player("Unranked", "B2")
    riot, service = build_service(**{"Ranked#A1": ranked, "Unranked#B2": unranked})

    comparison = await service.compare("Ranked#A1", "Unranked#B2")

    assert comparison.winner is ranked


async def test_compare_fails_when_either_player_is_missing():
    known = make_player("Known", "A1", solo=solo_entry())
    riot, service = build_service(**{"Known#A1": known})

    with pytest.raises(RiotLookupError) as raised:
        await service.compare("Known#A1", "Ghost#0000")

    assert "Ghost#0000" in str(raised.value)


def test_rank_summary_of_ranked_entry_includes_tier_points_record_and_winrate():
    summary = rank_summary(solo_entry("GOLD", "II", 45, 20, 15))

    assert summary == "Gold II, 45 LP, 20V / 15D (57% winrate)"


def test_rank_summary_without_entry_says_unranked():
    assert rank_summary(None) == "Sin clasificar"
