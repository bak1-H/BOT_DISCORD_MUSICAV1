import pytest
from langchain_core.messages import HumanMessage

from agent.model import (
    DEFAULT_MODEL,
    AgentConfigError,
    build_chat_model,
    build_runner,
    resolve_model_name,
    sampling_options,
)
from agent.runner import LangGraphAgentRunner
from agent.tools import build_registry


def test_model_name_precedence_agent_then_shared_then_default():
    assert resolve_model_name({"GEMINI_AGENT_MODEL": "agent-m", "GEMINI_MODEL": "shared-m"}) == "agent-m"
    assert resolve_model_name({"GEMINI_MODEL": "shared-m"}) == "shared-m"
    assert resolve_model_name({"GEMINI_AGENT_MODEL": "  ", "GEMINI_MODEL": ""}) == DEFAULT_MODEL
    assert DEFAULT_MODEL == "gemini-3.5-flash-lite"


def test_missing_api_key_raises_config_error():
    with pytest.raises(AgentConfigError):
        build_chat_model(build_registry(), {"GEMINI_API_KEY": "  "})


def test_chat_model_is_built_with_design_parameters_and_bound_tools():
    registry = build_registry()

    bound = build_chat_model(registry, {"GEMINI_API_KEY": "fake-key", "GEMINI_AGENT_MODEL": "gemini-test"})

    chat = bound.bound
    assert chat.temperature == 0.2
    assert chat.timeout == 20
    assert chat.max_retries == 1
    assert chat.model.endswith("gemini-test")
    assert sorted(tool["function"]["name"] for tool in bound.kwargs["tools"]) == sorted(registry.names())


def test_gemini_3_models_leave_temperature_to_the_provider_defaults():
    bound = build_chat_model(build_registry(), {"GEMINI_API_KEY": "fake-key"})

    assert bound.bound.temperature is None
    assert bound.bound.model.endswith(DEFAULT_MODEL)
    assert sampling_options("gemini-2.5-flash") == {"temperature": 0.2}
    assert sampling_options("gemini-3.6-flash") == {}


def test_every_tool_schema_converts_to_a_gemini_function_declaration_offline():
    registry = build_registry()
    bound = build_chat_model(registry, {"GEMINI_API_KEY": "fake-key", "GEMINI_AGENT_MODEL": "gemini-test"})

    request = bound.bound._prepare_request(messages=[HumanMessage("hola")], **bound.kwargs)

    declared = [declaration.name for declaration in request["config"].tools[0].function_declarations]
    assert sorted(declared) == sorted(registry.names())


def test_build_runner_returns_langgraph_runner():
    runner = build_runner({"GEMINI_API_KEY": "fake-key"})

    assert isinstance(runner, LangGraphAgentRunner)
