import asyncio
import json
from types import SimpleNamespace

import pytest

import ai_dj


class FakeGeminiModels:
    def __init__(self, reply=None, error=None, delay=0):
        self.reply = reply
        self.error = error
        self.delay = delay
        self.requests = []

    async def generate_content(self, *, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.reply)


class FakeGeminiClient:
    def __init__(self, models):
        self.aio = SimpleNamespace(models=models)


@pytest.fixture
def gemini(monkeypatch):
    def install(reply=None, error=None, delay=0):
        models = FakeGeminiModels(reply=reply, error=error, delay=delay)
        monkeypatch.setattr(ai_dj, "_client", FakeGeminiClient(models))
        return models

    monkeypatch.setattr(ai_dj, "_client", None)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GEMINI_MODEL", "")
    return install


def songs_reply(*songs):
    return json.dumps({"songs": list(songs)})


def plan_reply(intent="playlist", summary="10 de Feid", songs=(), max_duration_s=600):
    return json.dumps({
        "intent": intent,
        "summary": summary,
        "songs": list(songs),
        "max_duration_s": max_duration_s,
    })


async def test_suggest_songs_returns_cleaned_songs_limited_to_count(gemini):
    models = gemini(songs_reply("  Feid - Normal ", "", 42, "Karol G - Tusa", "Bad Bunny - Dakiti"))

    result = await ai_dj.suggest_songs("reggaeton", ["Old song"], 2)

    assert result == ["Feid - Normal", "Karol G - Tusa"]
    prompt = models.requests[0]["contents"]
    assert "reggaeton" in prompt
    assert "- Old song" in prompt
    assert "Suggest 2 different songs" in prompt


async def test_suggest_songs_marks_empty_history_in_prompt(gemini):
    models = gemini(songs_reply("A - B"))

    await ai_dj.suggest_songs("pop", [], 1)

    assert "- (none yet)" in models.requests[0]["contents"]


async def test_model_defaults_when_env_is_empty(gemini):
    models = gemini(songs_reply("A - B"))

    await ai_dj.suggest_songs("pop", [], 1)

    assert models.requests[0]["model"] == ai_dj.DEFAULT_MODEL


async def test_model_comes_from_env_when_set(gemini, monkeypatch):
    models = gemini(songs_reply("A - B"))
    monkeypatch.setenv("GEMINI_MODEL", "custom-model")

    await ai_dj.suggest_songs("pop", [], 1)

    assert models.requests[0]["model"] == "custom-model"


async def test_suggest_songs_returns_empty_when_gemini_is_not_configured(gemini):
    assert await ai_dj.suggest_songs("pop", [], 3) == []


async def test_suggest_songs_returns_empty_on_invalid_json(gemini):
    gemini("not json at all")

    assert await ai_dj.suggest_songs("pop", [], 3) == []


async def test_suggest_songs_returns_empty_when_payload_is_not_an_object(gemini):
    gemini(json.dumps(["A - B"]))

    assert await ai_dj.suggest_songs("pop", [], 3) == []


async def test_suggest_songs_returns_empty_when_songs_is_not_a_list(gemini):
    gemini(json.dumps({"songs": "A - B"}))

    assert await ai_dj.suggest_songs("pop", [], 3) == []


async def test_suggest_songs_returns_empty_when_gemini_raises(gemini):
    gemini(error=RuntimeError("429 quota"))

    assert await ai_dj.suggest_songs("pop", [], 3) == []


async def test_suggest_songs_returns_empty_on_timeout(gemini, monkeypatch):
    gemini(songs_reply("A - B"), delay=1)
    monkeypatch.setattr(ai_dj, "RADIO_TIMEOUT_S", 0.01)

    assert await ai_dj.suggest_songs("pop", [], 3) == []


async def test_plan_playlist_returns_playlist_intent_with_songs(gemini):
    models = gemini(plan_reply(songs=["Feid - Normal", "Feid - Luna"], max_duration_s=300))

    plan = await ai_dj.plan_playlist("10 de Feid", 15, 600)

    assert plan.is_playlist
    assert plan.summary == "10 de Feid"
    assert plan.songs == ["Feid - Normal", "Feid - Luna"]
    assert plan.max_duration_s == 300
    assert "never more than 15" in models.requests[0]["contents"]


async def test_plan_playlist_limits_songs_to_max_songs(gemini):
    gemini(plan_reply(songs=[f"A - {i}" for i in range(20)]))

    plan = await ai_dj.plan_playlist("muchas", 15, 600)

    assert len(plan.songs) == 15


async def test_plan_playlist_unsupported_intent_drops_songs(gemini):
    gemini(plan_reply(intent="unsupported", summary="Quieres sacar la 10", songs=["A - B"]))

    plan = await ai_dj.plan_playlist("saca la 10", 15, 600)

    assert not plan.is_playlist
    assert plan.songs == []
    assert plan.summary == "Quieres sacar la 10"


async def test_plan_playlist_unknown_intent_becomes_unsupported(gemini):
    gemini(plan_reply(intent="weird", songs=["A - B"]))

    plan = await ai_dj.plan_playlist("???", 15, 600)

    assert plan.intent == ai_dj.UNSUPPORTED_INTENT
    assert plan.songs == []


@pytest.mark.parametrize("invalid_duration", [0, -5, "300", None])
async def test_plan_playlist_falls_back_to_default_duration(gemini, invalid_duration):
    gemini(plan_reply(songs=["A - B"], max_duration_s=invalid_duration))

    plan = await ai_dj.plan_playlist("algo", 15, 777)

    assert plan.max_duration_s == 777


async def test_plan_playlist_returns_none_when_unavailable(gemini):
    assert await ai_dj.plan_playlist("algo", 15, 600) is None


async def test_plan_playlist_returns_none_when_gemini_raises(gemini):
    gemini(error=RuntimeError("boom"))

    assert await ai_dj.plan_playlist("algo", 15, 600) is None


def test_playlist_plan_without_songs_is_not_a_playlist():
    plan = ai_dj.PlaylistPlan(intent=ai_dj.PLAYLIST_INTENT, summary="", songs=[], max_duration_s=600)

    assert not plan.is_playlist
