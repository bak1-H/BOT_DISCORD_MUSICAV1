import asyncio

import pytest

from lol.riot import MISSING_API_KEY_MESSAGE, RiotApi, RiotLookupError
from tests.lol_support import (
    PLATFORM,
    ROUTING,
    FakeResponse,
    account_payload,
    flex_entry,
    network_failure,
    ok_routes,
    session_factory_for,
    solo_entry,
)

ACCOUNT_ROUTE = "/riot/account/v1/accounts/by-riot-id/"
SUMMONER_ROUTE = "/lol/summoner/v4/summoners/by-puuid/"
LEAGUE_ROUTE = "/lol/league/v4/entries/by-puuid/"


def build_api(routes, api_key="RGAPI-test"):
    session, factory = session_factory_for(routes)
    return session, RiotApi(api_key, PLATFORM, ROUTING, session_factory=factory)


async def test_successful_lookup_returns_player_with_ranks_and_level():
    session, api = build_api(ok_routes())

    player = await api.fetch_player("Faker#KR1")

    assert (player.game_name, player.tag_line, player.level, player.icon_id) == ("Faker", "KR1", 312, 29)
    assert player.solo == solo_entry()
    assert player.flex == flex_entry()
    assert len(player.entries) == 2


async def test_requests_use_routing_for_accounts_and_platform_for_the_rest_with_token_header():
    session, api = build_api(ok_routes())

    await api.fetch_player("Faker#KR1")

    urls = [url for url, headers in session.requests]
    assert urls[0].startswith(f"https://{ROUTING}.api.riotgames.com{ACCOUNT_ROUTE}Faker/KR1")
    assert urls[1].startswith(f"https://{PLATFORM}.api.riotgames.com{SUMMONER_ROUTE}puuid-1")
    assert urls[2].startswith(f"https://{PLATFORM}.api.riotgames.com{LEAGUE_ROUTE}puuid-1")
    assert all(headers == {"X-Riot-Token": "RGAPI-test"} for url, headers in session.requests)


async def test_unknown_account_raises_not_found_naming_the_riot_id():
    session, api = build_api({ACCOUNT_ROUTE: FakeResponse(404)})

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Nadie#0000")

    assert "Nadie#0000" in str(raised.value)
    assert "no encontrado" in str(raised.value)
    assert len(session.requests) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key_raises_invalid_key_message(status):
    session, api = build_api({ACCOUNT_ROUTE: FakeResponse(status)})

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Faker#KR1")

    assert "API key inválida" in str(raised.value)


async def test_other_account_status_is_reported_with_its_code():
    session, api = build_api({ACCOUNT_ROUTE: FakeResponse(500)})

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Faker#KR1")

    assert "500" in str(raised.value)


async def test_missing_api_key_fails_before_any_request():
    session, api = build_api(ok_routes(), api_key="")

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Faker#KR1")

    assert str(raised.value) == MISSING_API_KEY_MESSAGE
    assert session.requests == []


async def test_network_failure_raises_friendly_error():
    session, api = build_api({ACCOUNT_ROUTE: network_failure()})

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Faker#KR1")

    assert "red" in str(raised.value)


async def test_ranked_endpoint_404_means_unranked_player():
    routes = ok_routes()
    routes[LEAGUE_ROUTE] = FakeResponse(404)
    session, api = build_api(routes)

    player = await api.fetch_player("Faker#KR1")

    assert player.solo is None and player.flex is None and player.entries == []


async def test_summoner_endpoint_404_means_unknown_level():
    routes = ok_routes()
    routes[SUMMONER_ROUTE] = FakeResponse(404)
    session, api = build_api(routes)

    player = await api.fetch_player("Faker#KR1")

    assert player.level is None
    assert player.icon_id == 0


async def test_rate_limited_ranked_endpoint_raises_instead_of_faking_unranked():
    routes = ok_routes()
    routes[LEAGUE_ROUTE] = FakeResponse(429)
    session, api = build_api(routes)

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Faker#KR1")

    assert "límite" in str(raised.value)


async def test_rate_limited_account_endpoint_raises_rate_limit_message():
    session, api = build_api({ACCOUNT_ROUTE: FakeResponse(429)})

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Faker#KR1")

    assert "límite" in str(raised.value)


async def test_server_error_on_summoner_endpoint_raises():
    routes = ok_routes()
    routes[SUMMONER_ROUTE] = FakeResponse(500)
    session, api = build_api(routes)

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Faker#KR1")

    assert "500" in str(raised.value)


@pytest.mark.parametrize("route", [SUMMONER_ROUTE, LEAGUE_ROUTE])
async def test_rejected_key_on_secondary_endpoints_raises_invalid_key(route):
    routes = ok_routes()
    routes[route] = FakeResponse(403)
    session, api = build_api(routes)

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Faker#KR1")

    assert "API key inválida" in str(raised.value)


@pytest.mark.parametrize("route", [ACCOUNT_ROUTE, SUMMONER_ROUTE, LEAGUE_ROUTE])
async def test_malformed_json_raises_lookup_error(route):
    routes = ok_routes()
    routes[route] = FakeResponse(200, error=ValueError("not json"))
    session, api = build_api(routes)

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Faker#KR1")

    assert "inesperada" in str(raised.value)


@pytest.mark.parametrize("missing", ["puuid", "gameName", "tagLine"])
async def test_account_payload_missing_keys_raises_lookup_error(missing):
    account = account_payload()
    del account[missing]
    session, api = build_api(ok_routes(account=account))

    with pytest.raises(RiotLookupError):
        await api.fetch_player("Faker#KR1")


@pytest.mark.parametrize("missing", ["tier", "leaguePoints", "wins", "losses"])
async def test_ranked_entry_missing_keys_raises_lookup_error(missing):
    entry = solo_entry()
    del entry[missing]
    session, api = build_api(ok_routes(entries=[entry]))

    with pytest.raises(RiotLookupError):
        await api.fetch_player("Faker#KR1")


@pytest.mark.parametrize(
    "route, payload",
    [(ACCOUNT_ROUTE, ["x"]), (SUMMONER_ROUTE, ["x"]), (LEAGUE_ROUTE, {"a": 1}), (LEAGUE_ROUTE, ["x"])],
)
async def test_payloads_with_unexpected_shape_raise_lookup_error(route, payload):
    routes = ok_routes()
    routes[route] = FakeResponse(200, payload)
    session, api = build_api(routes)

    with pytest.raises(RiotLookupError):
        await api.fetch_player("Faker#KR1")


async def test_timeout_raises_friendly_error():
    session, api = build_api({ACCOUNT_ROUTE: asyncio.TimeoutError()})

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Faker#KR1")

    assert "red" in str(raised.value)


@pytest.mark.parametrize(
    "routes",
    [
        {ACCOUNT_ROUTE: FakeResponse(401)},
        {ACCOUNT_ROUTE: FakeResponse(429)},
        {ACCOUNT_ROUTE: FakeResponse(500)},
        {ACCOUNT_ROUTE: FakeResponse(404)},
        {ACCOUNT_ROUTE: network_failure()},
        {ACCOUNT_ROUTE: asyncio.TimeoutError()},
        {ACCOUNT_ROUTE: FakeResponse(200, error=ValueError("bad"))},
    ],
)
async def test_api_key_never_appears_in_error_messages(routes):
    session, api = build_api(routes, api_key="RGAPI-super-secret")

    with pytest.raises(RiotLookupError) as raised:
        await api.fetch_player("Faker#KR1")

    assert "RGAPI-super-secret" not in str(raised.value)


async def test_riot_id_without_tag_is_rejected_without_requests():
    session, api = build_api(ok_routes())

    with pytest.raises(RiotLookupError):
        await api.fetch_player("Faker")

    assert session.requests == []


async def test_path_characters_in_riot_id_are_escaped():
    session, api = build_api(ok_routes())

    await api.fetch_player("a/b?c#KR1")

    first_url = session.requests[0][0]
    assert "a%2Fb%3Fc/KR1" in first_url
