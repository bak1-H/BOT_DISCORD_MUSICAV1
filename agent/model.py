import os

from agent.runner import LangGraphAgentRunner
from agent.tools import ToolRegistry, build_registry

DEFAULT_MODEL = "gemini-3.5-flash-lite"
TEMPERATURE = 0.2
REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 2
FIXED_SAMPLING_FAMILY = "gemini-3"


class AgentConfigError(Exception):
    pass


def resolve_model_name(env) -> str:
    for key in ("GEMINI_AGENT_MODEL", "GEMINI_MODEL"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    return DEFAULT_MODEL


def sampling_options(model_name: str) -> dict:
    if FIXED_SAMPLING_FAMILY in model_name:
        return {}
    return {"temperature": TEMPERATURE}


def build_chat_model(registry: ToolRegistry, env=os.environ):
    api_key = (env.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise AgentConfigError("GEMINI_API_KEY no está configurada")

    from langchain_google_genai import ChatGoogleGenerativeAI

    model_name = resolve_model_name(env)
    chat = ChatGoogleGenerativeAI(
        model=model_name,
        api_key=api_key,
        **sampling_options(model_name),
        timeout=REQUEST_TIMEOUT_S,
        max_retries=MAX_RETRIES,
    )
    return chat.bind_tools(registry.schemas())


def build_runner(env=os.environ) -> LangGraphAgentRunner:
    registry = build_registry()
    return LangGraphAgentRunner(build_chat_model(registry, env), registry)
