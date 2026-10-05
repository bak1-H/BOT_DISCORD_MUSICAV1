import asyncio
from dataclasses import dataclass
from typing import Protocol

from lol.riot import LolPlayer

TIER_ORDER = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
DIVISION_BONUS = {"I": 3, "II": 2, "III": 1, "IV": 0}
UNRANKED = "Sin clasificar"
WIN = "🟢"
LOSS = "🔴"
TIE = "⚪"


class RiotPort(Protocol):
    async def fetch_player(self, riot_id: str) -> LolPlayer: ...


@dataclass(frozen=True)
class Comparison:
    first: LolPlayer
    second: LolPlayer
    solo_rank: tuple[str, str]
    solo_winrate: tuple[str, str]
    flex_rank: tuple[str, str]
    flex_winrate: tuple[str, str]
    points_first: int
    points_second: int
    winner: LolPlayer | None
    winner_tier: str | None


def best_tier(entries: list) -> str | None:
    tiers = [entry["tier"] for entry in entries if "tier" in entry]
    return max(tiers, key=lambda tier: TIER_ORDER.index(tier) if tier in TIER_ORDER else -1, default=None)


def winrate_percent(entry: dict | None) -> float:
    if not entry:
        return -1.0
    wins, losses = entry["wins"], entry["losses"]
    return round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0.0


def rank_score(entry: dict | None) -> float:
    if not entry:
        return -1.0
    tier_index = TIER_ORDER.index(entry["tier"]) if entry["tier"] in TIER_ORDER else 0
    return tier_index * 4 + DIVISION_BONUS.get(entry.get("rank", "IV"), 0) + entry["leaguePoints"] / 100


def rank_summary(entry: dict | None) -> str:
    if not entry:
        return UNRANKED
    division = f" {entry['rank']}" if entry.get("rank") else ""
    wins, losses = entry["wins"], entry["losses"]
    return (
        f"{entry['tier'].capitalize()}{division}, {entry['leaguePoints']} LP, "
        f"{wins}V / {losses}D ({winrate_percent(entry):.0f}% winrate)"
    )


def indicators(first: float, second: float) -> tuple[str, str]:
    if first > second:
        return WIN, LOSS
    if second > first:
        return LOSS, WIN
    return TIE, TIE


def points_for(marks: list) -> int:
    return sum(2 if mark == WIN else (1 if mark == TIE else 0) for mark in marks)


def compare_players(first: LolPlayer, second: LolPlayer) -> Comparison:
    solo_rank = indicators(rank_score(first.solo), rank_score(second.solo))
    solo_winrate = indicators(winrate_percent(first.solo), winrate_percent(second.solo))
    flex_rank = indicators(rank_score(first.flex), rank_score(second.flex))
    flex_winrate = indicators(winrate_percent(first.flex), winrate_percent(second.flex))
    points_first = points_for([solo_rank[0], solo_winrate[0], flex_rank[0], flex_winrate[0]])
    points_second = points_for([solo_rank[1], solo_winrate[1], flex_rank[1], flex_winrate[1]])
    if points_first > points_second:
        winner, winner_tier = first, best_tier(first.entries)
    elif points_second > points_first:
        winner, winner_tier = second, best_tier(second.entries)
    else:
        winner, winner_tier = None, best_tier(first.entries + second.entries)
    return Comparison(
        first=first,
        second=second,
        solo_rank=solo_rank,
        solo_winrate=solo_winrate,
        flex_rank=flex_rank,
        flex_winrate=flex_winrate,
        points_first=points_first,
        points_second=points_second,
        winner=winner,
        winner_tier=winner_tier,
    )


class LolService:
    def __init__(self, riot: RiotPort) -> None:
        self._riot = riot

    async def summoner(self, riot_id: str) -> LolPlayer:
        return await self._riot.fetch_player(riot_id)

    async def compare(self, riot_id_a: str, riot_id_b: str) -> Comparison:
        first, second = await asyncio.gather(self._riot.fetch_player(riot_id_a), self._riot.fetch_player(riot_id_b))
        return compare_players(first, second)
