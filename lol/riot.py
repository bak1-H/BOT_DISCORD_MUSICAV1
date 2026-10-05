from dataclasses import dataclass, field
from urllib.parse import quote

import aiohttp

REQUEST_TIMEOUT_S = 10
SOLO_QUEUE = "RANKED_SOLO_5x5"
FLEX_QUEUE = "RANKED_FLEX_SR"

MISSING_API_KEY_MESSAGE = "RIOT_API_KEY no configurada en el servidor."
INVALID_API_KEY_MESSAGE = "API key inválida o expirada."
NETWORK_ERROR_MESSAGE = "Error de red al consultar la API de Riot."
RATE_LIMIT_MESSAGE = "Riot alcanzó su límite de consultas por ahora. Intenta de nuevo en unos segundos."
MALFORMED_RESPONSE_MESSAGE = "Riot devolvió una respuesta inesperada. Intenta de nuevo más tarde."
REQUIRED_ACCOUNT_KEYS = ("puuid", "gameName", "tagLine")
REQUIRED_RANKED_KEYS = ("tier", "leaguePoints", "wins", "losses")


class RiotLookupError(Exception):
    pass


@dataclass(frozen=True)
class LolPlayer:
    game_name: str
    tag_line: str
    level: int | None
    icon_id: int
    solo: dict | None
    flex: dict | None
    entries: list = field(default_factory=list)

    @property
    def riot_id(self) -> str:
        return f"{self.game_name}#{self.tag_line}"


def status_error(status: int) -> RiotLookupError:
    if status in (401, 403):
        return RiotLookupError(INVALID_API_KEY_MESSAGE)
    if status == 429:
        return RiotLookupError(RATE_LIMIT_MESSAGE)
    return RiotLookupError(f"Error Riot API ({status}).")


async def read_json(response):
    try:
        return await response.json()
    except (ValueError, aiohttp.ContentTypeError) as error:
        raise RiotLookupError(MALFORMED_RESPONSE_MESSAGE) from error


def require_keys(payload, keys) -> dict:
    if not isinstance(payload, dict) or any(key not in payload for key in keys):
        raise RiotLookupError(MALFORMED_RESPONSE_MESSAGE)
    return payload


def default_session_factory():
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S))


def split_riot_id(riot_id: str) -> tuple[str, str]:
    game_name, separator, tag_line = riot_id.rpartition("#")
    if not separator or not game_name or not tag_line:
        raise RiotLookupError(f"`{riot_id}` no es un Riot ID válido; usa Nombre#TAG.")
    return game_name, tag_line


class RiotApi:
    def __init__(self, api_key: str, platform: str, routing: str, session_factory=default_session_factory) -> None:
        self._api_key = api_key
        self._platform = platform
        self._routing = routing
        self._session_factory = session_factory

    async def fetch_player(self, riot_id: str) -> LolPlayer:
        game_name, tag_line = split_riot_id(riot_id)
        if not self._api_key:
            raise RiotLookupError(MISSING_API_KEY_MESSAGE)
        headers = {"X-Riot-Token": self._api_key}
        try:
            async with self._session_factory() as session:
                account = await self._fetch_account(session, headers, riot_id, game_name, tag_line)
                summoner = await self._fetch_optional(
                    session,
                    headers,
                    f"https://{self._platform}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{quote(str(account['puuid']), safe='')}",
                    {},
                )
                entries = await self._fetch_optional(
                    session,
                    headers,
                    f"https://{self._platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{quote(str(account['puuid']), safe='')}",
                    [],
                )
        except (aiohttp.ClientError, TimeoutError) as error:
            print(f"Riot API error: {type(error).__name__}")
            raise RiotLookupError(NETWORK_ERROR_MESSAGE) from error
        summoner = require_keys(summoner, ())
        solo, flex = self._select_ranked(entries)
        return LolPlayer(
            game_name=account["gameName"],
            tag_line=account["tagLine"],
            level=summoner.get("summonerLevel"),
            icon_id=summoner.get("profileIconId", 0),
            solo=solo,
            flex=flex,
            entries=entries,
        )

    def _select_ranked(self, entries) -> tuple[dict | None, dict | None]:
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise RiotLookupError(MALFORMED_RESPONSE_MESSAGE)
        solo = next((entry for entry in entries if entry.get("queueType") == SOLO_QUEUE), None)
        flex = next((entry for entry in entries if entry.get("queueType") == FLEX_QUEUE), None)
        for entry in (solo, flex):
            if entry is not None:
                require_keys(entry, REQUIRED_RANKED_KEYS)
        return solo, flex

    async def _fetch_account(self, session, headers, riot_id, game_name, tag_line) -> dict:
        url = (
            f"https://{self._routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
            f"{quote(game_name, safe='')}/{quote(tag_line, safe='')}"
        )
        async with session.get(url, headers=headers) as response:
            if response.status == 404:
                raise RiotLookupError(f"`{riot_id}` no encontrado.")
            if response.status != 200:
                raise status_error(response.status)
            return require_keys(await read_json(response), REQUIRED_ACCOUNT_KEYS)

    async def _fetch_optional(self, session, headers, url, fallback):
        async with session.get(url, headers=headers) as response:
            if response.status == 404:
                return fallback
            if response.status != 200:
                raise status_error(response.status)
            return await read_json(response)
