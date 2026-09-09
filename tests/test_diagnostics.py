import io
import json
import logging
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from azure.ai.agentserver.responses import InMemoryResponseProvider
from azure.core.exceptions import ClientAuthenticationError
from azure.monitor.opentelemetry.exporter.export.trace._exporter import _convert_span_to_envelope
from openai import BadRequestError, PermissionDeniedError
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from comparison.config import load_config
from comparison.contracts import CompareRequest
from comparison.diagnostics import configure_safe_logging, failure, from_metadata, record_failure
from comparison.service import ComparisonService
from conftest import fake_client, fake_stream_client
from hosted_agent.app import create_app as hosted_app
from web.app import create_app

REQUEST_ID = "fa9f1d39d23daad8fbfb6c2607801dd6"
COMPARISON_ID = "b42b904d-b3f3-4214-a7f6-786b02b91f03"
PRIVATE = "PRIVATE-body-token-url-never-log"


def sdk_error(cls=BadRequestError, status=400):
    response = httpx2.Response(
        status, request=httpx2.Request("POST", f"https://example.invalid/{PRIVATE}"),
        headers={"x-request-id": REQUEST_ID, "Authorization": f"Bearer {PRIVATE}"},
    )
    return cls(PRIVATE, response=response, body={"message": PRIVATE, "input": PRIVATE})


@pytest.fixture
def recorded_spans(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("safe-diagnostics-test")
    for module in ("comparison.telemetry", "comparison.service", "hosted_agent.app", "web.app"):
        monkeypatch.setattr(f"{module}.tracer", tracer)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    yield tracer, exporter
    provider.shutdown()


def test_structured_exception_diagnostics_exclude_content():
    diagnostic = failure("prompt.responses.create", sdk_error())
    assert diagnostic == {
        "stage": "prompt.responses.create", "type": "BadRequestError",
        "http_status": 400, "request_id": REQUEST_ID,
    }
    assert PRIVATE not in json.dumps(diagnostic)


@pytest.mark.parametrize("value", [None, {}, [], "<script>", "Bearer secret", "x" * 500])
def test_metadata_fields_reject_noncanonical_values(value):
    raw = {"metadata": {"failure_stage": value, "failure_type": value}}
    assert from_metadata(raw) is None
    raw = {"metadata": {
        "failure_stage": "hosted.model.stream", "failure_type": "UpstreamFailed",
        "failure_http_status": value, "failure_request_id": value,
    }}
    diagnostic = from_metadata(raw)
    assert diagnostic["http_status"] is None
    assert diagnostic["request_id"] is None


def test_console_records_are_bounded_idempotent_and_content_free(recorded_spans):
    tracer, exporter = recorded_spans
    configure_safe_logging()
    configure_safe_logging()
    logger = logging.getLogger("comparison.safe")
    consoles = [handler for handler in logger.handlers if getattr(handler, "_comparison_safe_console", False)]
    assert len(consoles) == 1
    output = io.StringIO()
    capture = logging.StreamHandler(output)
    logger.addHandler(capture)
    try:
        with tracer.start_as_current_span("agent.prompt") as span:
            record_failure(span, failure("prompt.responses.create", sdk_error()), COMPARISON_ID)
    finally:
        logger.removeHandler(capture)
    line = output.getvalue()
    assert len(line) < 1024
    data = json.loads(line)
    assert data["request_id"] == REQUEST_ID
    assert data["comparison_id"] == COMPARISON_ID
    assert PRIVATE not in line
    assert all(key not in data for key in ("body", "message", "headers", "url", "token"))
    exported = exporter.get_finished_spans()[0]
    assert exported.status.status_code == StatusCode.ERROR
    assert _convert_span_to_envelope(exported).data.base_data.success is False


async def test_prompt_sdk_error_survives_as_safe_status_and_error_span(raw_response, recorded_spans):
    _, exporter = recorded_spans
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    clients["prompt"].responses.create.side_effect = sdk_error()
    web = create_app(ComparisonService(clients, load_config()))
    async with web.router.lifespan_context(web):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://web") as client:
            result = (await client.post("/api/compare", json={"message": "Non-sensitive question"})).json()
    diagnostic = result["prompt"]["error_diagnostics"]
    assert diagnostic["stage"] == "prompt.responses.create"
    assert diagnostic["type"] == "BadRequestError"
    assert diagnostic["http_status"] == 400 and diagnostic["request_id"] == REQUEST_ID
    args = clients["prompt"].responses.create.call_args.kwargs
    assert "reasoning" not in args
    assert args["max_output_tokens"] == 4096
    assert args["include"] == ["reasoning.encrypted_content"]
    assert result["hosted"]["error"] is None
    assert PRIVATE not in json.dumps(result)
    for span in exporter.get_finished_spans():
        if span.name in ("agent.prompt", "comparison"):
            assert span.status.status_code == StatusCode.ERROR
            assert _convert_span_to_envelope(span).data.base_data.success is False


@pytest.mark.parametrize("failed_sides", [("prompt",), ("hosted",), ("prompt", "hosted")])
async def test_root_comparison_error_when_either_side_fails(raw_response, recorded_spans, failed_sides):
    _, exporter = recorded_spans
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    for side in failed_sides:
        clients[side].responses.create.side_effect = sdk_error()
    web = create_app(ComparisonService(clients, load_config()))
    async with web.router.lifespan_context(web):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://web") as caller:
            result = (await caller.post("/api/compare", json={"message": "Non-sensitive question"})).json()
    assert all(result[side]["error"] for side in failed_sides)
    root = next(span for span in exporter.get_finished_spans() if span.name == "comparison")
    assert root.status.status_code == StatusCode.ERROR
    assert _convert_span_to_envelope(root).data.base_data.success is False
    assert root.events == ()
    assert PRIVATE not in json.dumps(result)


async def test_prompt_conversation_exception_keeps_original_safe_type(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    clients["prompt"].conversations.create.side_effect = sdk_error(PermissionDeniedError, 403)
    result = await ComparisonService(clients, load_config()).compare(CompareRequest(message="Q"), COMPARISON_ID)
    diagnostic = result["prompt"]["error_diagnostics"]
    assert diagnostic["stage"] == "prompt.conversations.create"
    assert diagnostic["type"] == "PermissionDeniedError"
    assert diagnostic["http_status"] == 403
    clients["prompt"].responses.create.assert_not_awaited()


async def test_hosted_upstream_errors_reach_web_without_bodies(settings, raw_response, recorded_spans):
    _, exporter = recorded_spans
    model = fake_stream_client(raw_response)
    model.responses.create.side_effect = sdk_error(PermissionDeniedError, 403)
    host = hosted_app(model, settings, store=InMemoryResponseProvider())
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    async with host.router.lifespan_context(host):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://host") as caller:
            async def invoke(**kwargs):
                headers = kwargs.pop("extra_headers")
                raw = (await caller.post("/responses", json=kwargs, headers=headers)).json()
                return SimpleNamespace(model_dump=lambda **kwargs: raw)
            clients["hosted"].responses.create.side_effect = invoke
            result = await ComparisonService(clients, load_config()).compare(CompareRequest(message="Q"), COMPARISON_ID)
    diagnostic = result["hosted"]["error_diagnostics"]
    expected = "hosted.model.responses.create"
    assert diagnostic == {"stage": expected, "type": "PermissionDeniedError", "http_status": 403, "request_id": REQUEST_ID}
    assert result["prompt"]["error"] is None
    assert PRIVATE not in json.dumps(result)
    model.conversations.create.assert_not_awaited()
    for span in exporter.get_finished_spans():
        if span.name in ("hosted.model", "agent.hosted"):
            assert span.status.status_code == StatusCode.ERROR
            assert span.attributes["http.response.status_code"] == 403
            assert _convert_span_to_envelope(span).data.base_data.success is False


async def test_hosted_no_longer_requires_model_conversation_create(settings, raw_response):
    model = fake_stream_client(raw_response)
    model.conversations.create.side_effect = sdk_error(PermissionDeniedError, 403)
    host = hosted_app(model, settings, store=InMemoryResponseProvider())
    async with host.router.lifespan_context(host):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://host") as caller:
            result = (await caller.post("/responses", json={"input": "Q", "store": True})).json()
    assert result["status"] == "completed"
    assert result["id"]
    assert model.responses.create.call_args.kwargs["store"] is False
    model.conversations.create.assert_not_awaited()


@pytest.mark.parametrize("status", ["failed", "incomplete"])
async def test_hosted_terminal_failures_are_error_spans(settings, raw_response, recorded_spans, status):
    _, exporter = recorded_spans
    raw_response["status"] = status
    host = hosted_app(fake_stream_client(raw_response), settings, store=InMemoryResponseProvider())
    async with host.router.lifespan_context(host):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://host") as client:
            result = (await client.post("/responses", json={"input": "Q", "store": False})).json()
    diagnostic = from_metadata(result)
    assert diagnostic["stage"] == "hosted.model.stream"
    assert diagnostic["type"] == ("UpstreamFailed" if status == "failed" else "UpstreamIncomplete")
    span = next(span for span in exporter.get_finished_spans() if span.name == "hosted.model")
    assert span.status.status_code == StatusCode.ERROR
    assert _convert_span_to_envelope(span).data.base_data.success is False


def test_credential_failure_keeps_no_message_or_unknown_status():
    diagnostic = failure("hosted.model.conversations.create", ClientAuthenticationError(PRIVATE))
    assert diagnostic["type"] == "ClientAuthenticationError"
    assert diagnostic["http_status"] is None and diagnostic["request_id"] is None
    assert PRIVATE not in json.dumps(diagnostic)
