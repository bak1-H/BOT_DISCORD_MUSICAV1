import aiohttp

from lol.riot import LolPlayer, RiotLookupError

PLATFORM = "la2"
ROUTING = "americas"


class FakeResponse:
    def __init__(self, status=200, payload=None, error=None):
        self.status = status
        self.payload = payload
        self.error = error

    async def json(self):
        if self.error is not None:
            raise self.error
        return self.payload


class FakeRequest:
    def __init__(self, outcome):
        self.outcome = outcome

    async def __aenter__(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def get(self, url, headers=None):
        self.requests.append((url, headers))
        for fragment, outcome in self.routes.items():
            if fragment in url:
                return FakeRequest(outcome)
        return FakeRequest(FakeResponse(404))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def session_factory_for(routes):
    session = FakeSession(routes)
    return session, lambda: session


def account_payload(game_name="Faker", tag_line="KR1", puuid="puuid-1"):
    return {"puuid": puuid, "gameName": game_name, "tagLine": tag_line}


def league_entry(queue_type, tier, rank, points, wins, losses):
    return {
        "queueType": queue_type,
        "tier": tier,
        "rank": rank,
        "leaguePoints": points,
        "wins": wins,
        "losses": losses,
    }


def solo_entry(tier="GOLD", rank="II", points=45, wins=20, losses=15):
    return league_entry("RANKED_SOLO_5x5", tier, rank, points, wins, losses)


def flex_entry(tier="SILVER", rank="I", points=10, wins=5, losses=5):
    return league_entry("RANKED_FLEX_SR", tier, rank, points, wins, losses)


def ok_routes(entries=None, account=None, level=312):
    return {
        "/riot/account/v1/accounts/by-riot-id/": FakeResponse(200, account or account_payload()),
        "/lol/summoner/v4/summoners/by-puuid/": FakeResponse(200, {"summonerLevel": level, "profileIconId": 29}),
        "/lol/league/v4/entries/by-puuid/": FakeResponse(200, entries if entries is not None else [solo_entry(), flex_entry()]),
    }


def network_failure():
    return aiohttp.ClientConnectionError("boom")


def make_player(game_name="Faker", tag_line="KR1", solo=None, flex=None, level=312):
    entries = [entry for entry in (solo, flex) if entry]
    return LolPlayer(
        game_name=game_name,
        tag_line=tag_line,
        level=level,
        icon_id=29,
        solo=solo,
        flex=flex,
        entries=entries,
    )


class FakeRiotApi:
    def __init__(self, players=None):
        self.players = players if players is not None else {}
        self.queries = []

    async def fetch_player(self, riot_id):
        self.queries.append(riot_id)
        outcome = self.players.get(riot_id)
        if outcome is None:
            raise RiotLookupError(f"`{riot_id}` no encontrado.")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
