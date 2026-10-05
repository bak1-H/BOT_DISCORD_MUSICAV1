import asyncio

import httpx
import pytest
from google.genai.errors import ClientError, ServerError
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai.chat_models import GoogleRateLimitError, _handle_client_error, _handle_server_error

from agent.errors import ErrorKind, classify_error, user_message


def raised_by_wrapper(handler, error, request=None):
    try:
        handler(error, request) if request is not None else handler(error)
    except Exception as wrapped:
        return wrapped
    raise AssertionError("wrapper did not raise")


def test_real_wrapper_429_is_rate_limit():
    original = ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}})
    wrapped = raised_by_wrapper(_handle_client_error, original, {"model": "gemini-x"})
    assert isinstance(wrapped, GoogleRateLimitError)
    assert classify_error(wrapped) is ErrorKind.RATE_LIMIT


def test_bare_client_error_429_without_wrapper_is_rate_limit():
    assert classify_error(ClientError(429, {"error": {"message": "quota"}})) is ErrorKind.RATE_LIMIT


def test_real_wrapper_500_is_unavailable():
    original = ServerError(503, {"error": {"message": "overloaded"}})
    wrapped = raised_by_wrapper(_handle_server_error, original)
    assert classify_error(wrapped) is ErrorKind.UNAVAILABLE


def test_rate_limit_found_through_cause_chain():
    try:
        try:
            raise ClientError(429, {"error": {"message": "quota"}})
        except ClientError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as outer:
        assert classify_error(outer) is ErrorKind.RATE_LIMIT


def test_status_code_attribute_is_honored():
    class StatusError(Exception):
        status_code = 429

    assert classify_error(StatusError()) is ErrorKind.RATE_LIMIT


@pytest.mark.parametrize(
    "error",
    [asyncio.TimeoutError(), TimeoutError(), httpx.ReadTimeout("slow")],
)
def test_timeouts_are_classified(error):
    assert classify_error(error) is ErrorKind.TIMEOUT


def test_connection_failure_is_unavailable():
    assert classify_error(httpx.ConnectError("down")) is ErrorKind.UNAVAILABLE


def test_unknown_exception_is_other():
    assert classify_error(ValueError("boom")) is ErrorKind.OTHER


def test_messages_match_design_table():
    assert "límite de uso de Gemini" in user_message(ErrorKind.RATE_LIMIT)
    assert "tardó demasiado" in user_message(ErrorKind.TIMEOUT)
    assert "!ayuda" in user_message(ErrorKind.UNAVAILABLE)
    assert "divídelo en partes" in user_message(ErrorKind.TOOL_LIMIT)
    assert "Algo salió mal" in user_message(ErrorKind.OTHER)
    assert "!play" in user_message(ErrorKind.RATE_LIMIT)


def test_chat_model_accepts_documented_parameter_names():
    model = ChatGoogleGenerativeAI(model="gemini-3.5-flash-lite", api_key="x", timeout=20, max_retries=1, temperature=0.2)
    assert model.timeout == 20
    assert model.max_retries == 1
    assert model.temperature == 0.2
    assert model.model.endswith("gemini-3.5-flash-lite")
