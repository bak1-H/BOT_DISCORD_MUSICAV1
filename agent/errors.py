import asyncio
from enum import Enum

import httpx
from langchain_core.exceptions import ModelAPIError, ModelConnectionError, ModelRateLimitError, ModelTimeoutError

RATE_LIMIT_STATUS = 429
GATEWAY_TIMEOUT_STATUS = 504
DEADLINE_MARKER = "DEADLINE_EXCEEDED"
SERVER_ERROR_FROM = 500
MAX_CAUSE_DEPTH = 8


class ErrorKind(Enum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    TOOL_LIMIT = "tool_limit"
    OTHER = "other"


USER_MESSAGES = {
    ErrorKind.RATE_LIMIT: "Alcancé el límite de uso de Gemini por ahora. Intenta en un minuto o usa `!play`.",
    ErrorKind.TIMEOUT: "Gemini tardó demasiado en responder. Intenta de nuevo o usa `!play`.",
    ErrorKind.UNAVAILABLE: (
        "El asistente no está disponible en este momento. Puedes usar `!play`, `!skip` y `!stop`, o escribe `!ayuda`."
    ),
    ErrorKind.TOOL_LIMIT: "Ese pedido es muy largo; divídelo en partes.",
    ErrorKind.OTHER: "Algo salió mal procesando tu pedido.",
}


def user_message(kind: ErrorKind) -> str:
    return USER_MESSAGES[kind]


def _causes(error: BaseException):
    seen = set()
    current = error
    while current is not None and id(current) not in seen and len(seen) < MAX_CAUSE_DEPTH:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_of(error: BaseException) -> int | None:
    for attribute in ("status_code", "code"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            return value
    return None


def _is_deadline_exceeded(error: BaseException) -> bool:
    return _status_of(error) == GATEWAY_TIMEOUT_STATUS or DEADLINE_MARKER in str(error).upper()


def _classify_single(error: BaseException) -> ErrorKind | None:
    if isinstance(error, ModelRateLimitError) or _status_of(error) == RATE_LIMIT_STATUS:
        return ErrorKind.RATE_LIMIT
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException, ModelTimeoutError)):
        return ErrorKind.TIMEOUT
    status = _status_of(error)
    if isinstance(error, (ModelAPIError, ModelConnectionError, httpx.TransportError)):
        return ErrorKind.UNAVAILABLE
    if status is not None and status >= SERVER_ERROR_FROM:
        return ErrorKind.UNAVAILABLE
    return None


def classify_error(error: BaseException) -> ErrorKind:
    chain = list(_causes(error))
    if any(_is_deadline_exceeded(cause) for cause in chain):
        return ErrorKind.TIMEOUT
    for cause in chain:
        kind = _classify_single(cause)
        if kind is not None:
            return kind
    return ErrorKind.OTHER
