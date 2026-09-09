import json
import logging
import re
from collections.abc import Mapping

from opentelemetry.trace import Status, StatusCode

STAGES = frozenset({
    "request.validate", "continuation",
    "prompt.conversations.create", "prompt.responses.create", "prompt.response.parse",
    "hosted.responses.create", "hosted.response.parse",
    "hosted.model.input", "hosted.model.conversations.create",
    "hosted.model.responses.create", "hosted.model.stream", "hosted.model.response.validate",
    "unknown",
})
ERROR_TYPES = frozenset({
    "BadRequestError", "AuthenticationError", "PermissionDeniedError", "NotFoundError",
    "ConflictError", "UnprocessableEntityError", "RateLimitError", "InternalServerError",
    "APIStatusError", "APIConnectionError", "APITimeoutError", "APIResponseValidationError",
    "ClientAuthenticationError", "CredentialUnavailableError", "HttpResponseError",
    "ServiceRequestError", "ServiceResponseError", "TimeoutError", "ValueError",
    "HistoryExpired", "HistoryFull", "CancelledError", "CancelledStream", "InterruptedStream",
    "ConversationUnavailable", "UpstreamFailed", "UpstreamIncomplete", "UnexpectedError",
})
_REQUEST_ID = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})")
_LOGGER = logging.getLogger("comparison.safe")


def request_id(value):
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) else None


def http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def failure(stage, exc=None, *, kind=None, status=None, request=None):
    name = type(exc).__name__ if exc is not None else kind
    if exc is not None:
        status = getattr(exc, "status_code", None)
        request = getattr(exc, "request_id", None)
        response = getattr(exc, "response", None)
        if response is not None:
            status = status if status is not None else getattr(response, "status_code", None)
            if request is None:
                request = response_request_id(response)
    return {
        "stage": stage if isinstance(stage, str) and stage in STAGES else "unknown",
        "type": name if isinstance(name, str) and name in ERROR_TYPES else "UnexpectedError",
        "http_status": http_status(status),
        "request_id": request_id(request),
    }


def response_request_id(response):
    headers = getattr(response, "headers", {})
    if not isinstance(headers, Mapping):
        return None
    for name in ("x-request-id", "apim-request-id", "x-ms-request-id"):
        identifier = request_id(headers.get(name))
        if identifier:
            return identifier
    return None


def to_metadata(diagnostic):
    return {
        f"failure_{key}": str(value)
        for key, value in diagnostic.items() if value is not None
    }


def from_metadata(raw):
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    stage, kind = metadata.get("failure_stage"), metadata.get("failure_type")
    if not isinstance(stage, str) or not isinstance(kind, str) or stage not in STAGES or kind not in ERROR_TYPES:
        return None
    status = metadata.get("failure_http_status")
    status = int(status) if isinstance(status, str) and re.fullmatch(r"[1-5][0-9]{2}", status) else None
    return failure(
        stage, kind=kind,
        status=status, request=metadata.get("failure_request_id"),
    )


def configure_safe_logging():
    if not any(getattr(handler, "_comparison_safe_console", False) for handler in _LOGGER.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._comparison_safe_console = True
        _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.WARNING)
    _LOGGER.propagate = False


def record_failure(span, diagnostic, comparison_id):
    diagnostic = failure(
        diagnostic.get("stage"), kind=diagnostic.get("type"),
        status=diagnostic.get("http_status"), request=diagnostic.get("request_id"),
    )
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute("comparison.success", False)
    span.set_attribute("comparison.failure.stage", diagnostic["stage"])
    span.set_attribute("error.type", diagnostic["type"])
    for key, attribute in (
        ("http_status", "http.response.status_code"),
        ("request_id", "comparison.failure.request_id"),
    ):
        if diagnostic[key] is not None:
            span.set_attribute(attribute, diagnostic[key])
    context = span.get_span_context()
    record = {
        "event": "comparison_failure",
        "comparison_id": request_id(comparison_id),
        **diagnostic,
        "trace_id": format(context.trace_id, "032x") if context.is_valid else None,
        "span_id": format(context.span_id, "016x") if context.is_valid else None,
    }
    configure_safe_logging()
    # Fixed keys and bounded allowlisted values: never format an exception here.
    _LOGGER.warning("%s", json.dumps(record, separators=(",", ":")))
