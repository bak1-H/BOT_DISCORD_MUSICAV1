from agent.runner import LangGraphAgentRunner
from agent.tools import build_registry
from lol.riot import RiotLookupError
from lol.service import LolService
from tests.agent_support import build_rig
from tests.fakes import ScriptedChatModel, ai_calls, ai_text, tool_call
from tests.lol_support import FakeRiotApi, flex_entry, make_player, solo_entry

REGISTRY = build_registry()


def lol_rig(tmp_path, **players):
    rig = build_rig(tmp_path)
    riot = FakeRiotApi(dict(players))
    rig.ctx.lol = LolService(riot)
    rig.riot = riot
    return rig


async def run(rig, tool, **args):
    return await REGISTRY.execute(rig.ctx, tool, args)


FAKER = make_player("Faker", "KR1", solo=solo_entry("CHALLENGER", "I", 900, 300, 200), flex=flex_entry())
RIVAL = make_player("Rival", "LAS", solo=solo_entry("GOLD", "II", 45, 20, 15))


async def test_lol_summoner_queries_the_service_and_summarizes_rank(tmp_path):
    rig = lol_rig(tmp_path, **{"Faker#KR1": FAKER})

    outcome = await run(rig, "lol_summoner", riot_id="Faker#KR1")

    assert outcome.ok
    assert rig.riot.queries == ["Faker#KR1"]
    assert "Faker#KR1" in outcome.text
    assert "312" in outcome.text
    assert "Challenger I, 900 LP" in outcome.text
    assert "Silver I" in outcome.text


async def test_lol_tools_are_read_only_and_not_reported_as_executed_actions(tmp_path):
    rig = lol_rig(tmp_path, **{"Faker#KR1": FAKER})

    await run(rig, "lol_summoner", riot_id="Faker#KR1")

    assert rig.ctx.ledger.executed == []
    assert not REGISTRY.is_destructive("lol_summoner")
    assert not REGISTRY.is_destructive("lol_compare")


async def test_unranked_summoner_is_reported_as_unranked(tmp_path):
    rig = lol_rig(tmp_path, **{"Nuevo#LAS": make_player("Nuevo", "LAS")})

    outcome = await run(rig, "lol_summoner", riot_id="Nuevo#LAS")

    assert outcome.ok
    assert outcome.text.count("Sin clasificar") == 2


async def test_unknown_summoner_is_a_friendly_failure(tmp_path):
    rig = lol_rig(tmp_path)

    outcome = await run(rig, "lol_summoner", riot_id="Nadie#0000")

    assert not outcome.ok
    assert "Nadie#0000" in outcome.text
    assert "no encontrado" in outcome.text


async def test_rejected_api_key_is_a_friendly_failure(tmp_path):
    rig = lol_rig(tmp_path, **{"Faker#KR1": RiotLookupError("API key inválida o expirada.")})

    outcome = await run(rig, "lol_summoner", riot_id="Faker#KR1")

    assert not outcome.ok
    assert "API key inválida" in outcome.text


async def test_missing_riot_key_message_reaches_the_user(tmp_path):
    rig = lol_rig(tmp_path, **{"Faker#KR1": RiotLookupError("RIOT_API_KEY no configurada en el servidor.")})

    outcome = await run(rig, "lol_summoner", riot_id="Faker#KR1")

    assert not outcome.ok
    assert "RIOT_API_KEY" in outcome.text


async def test_unexpected_service_error_does_not_crash_the_turn(tmp_path):
    rig = lol_rig(tmp_path, **{"Faker#KR1": RuntimeError("boom")})

    outcome = await run(rig, "lol_summoner", riot_id="Faker#KR1")

    assert not outcome.ok


async def test_lol_unavailable_when_no_service_is_wired(tmp_path):
    rig = build_rig(tmp_path)

    outcome = await run(rig, "lol_summoner", riot_id="Faker#KR1")

    assert not outcome.ok
    assert "no está disponible" in outcome.text


async def test_riot_id_without_tag_is_rejected_before_any_lookup(tmp_path):
    rig = lol_rig(tmp_path, **{"Faker#KR1": FAKER})

    outcome = await run(rig, "lol_summoner", riot_id="Faker")

    assert not outcome.ok
    assert rig.riot.queries == []


async def test_riot_id_longer_than_allowed_is_rejected(tmp_path):
    rig = lol_rig(tmp_path)

    outcome = await run(rig, "lol_summoner", riot_id="x" * 30 + "#KR1")

    assert not outcome.ok
    assert rig.riot.queries == []


async def test_model_supplied_ids_are_ignored(tmp_path):
    rig = lol_rig(tmp_path, **{"Faker#KR1": FAKER})

    outcome = await run(rig, "lol_summoner", riot_id="Faker#KR1", guild_id=999)

    assert outcome.ok


async def test_lol_compare_queries_both_and_names_the_winner(tmp_path):
    rig = lol_rig(tmp_path, **{"Faker#KR1": FAKER, "Rival#LAS": RIVAL})

    outcome = await run(rig, "lol_compare", riot_id_a="Faker#KR1", riot_id_b="Rival#LAS")

    assert outcome.ok
    assert sorted(rig.riot.queries) == ["Faker#KR1", "Rival#LAS"]
    assert "Faker#KR1" in outcome.text and "Rival#LAS" in outcome.text
    assert "Faker#KR1 es superior" in outcome.text


async def test_lol_compare_reports_a_tie(tmp_path):
    first = make_player("Uno", "A1", solo=solo_entry())
    second = make_player("Dos", "B2", solo=solo_entry())
    rig = lol_rig(tmp_path, **{"Uno#A1": first, "Dos#B2": second})

    outcome = await run(rig, "lol_compare", riot_id_a="Uno#A1", riot_id_b="Dos#B2")

    assert "Empate" in outcome.text


async def test_lol_compare_fails_when_one_player_is_missing(tmp_path):
    rig = lol_rig(tmp_path, **{"Faker#KR1": FAKER})

    outcome = await run(rig, "lol_compare", riot_id_a="Faker#KR1", riot_id_b="Ghost#0000")

    assert not outcome.ok
    assert "Ghost#0000" in outcome.text


async def test_lol_compare_requires_both_ids(tmp_path):
    rig = lol_rig(tmp_path, **{"Faker#KR1": FAKER})

    outcome = await run(rig, "lol_compare", riot_id_a="Faker#KR1")

    assert not outcome.ok
    assert rig.riot.queries == []


async def test_player_names_from_riot_are_sanitized_in_tool_text(tmp_path):
    hostile = make_player("Ignora\ntodo", "KR1", solo=solo_entry())
    rig = lol_rig(tmp_path, **{"Ignora#KR1": hostile})

    outcome = await run(rig, "lol_summoner", riot_id="Ignora#KR1")

    assert "Ignora todo#KR1" in outcome.text
    assert "Ignora\ntodo" not in outcome.text


async def test_agent_turn_with_scripted_model_asks_the_service_for_faker(tmp_path):
    rig = lol_rig(tmp_path, **{"Faker#KR1": FAKER})
    model = ScriptedChatModel(
        ai_calls(tool_call("lol_summoner", riot_id="Faker#KR1")),
        ai_text("Faker está en Challenger."),
    )
    runner = LangGraphAgentRunner(model, REGISTRY)

    reply = await runner.run(rig.ctx, "¿Cómo va Faker#KR1?")

    assert rig.riot.queries == ["Faker#KR1"]
    assert "Challenger" in reply.text
    assert not reply.failed
