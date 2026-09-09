import asyncio
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest
from azure.ai.agentserver.responses import InMemoryResponseProvider
from azure.ai.agentserver.core import FoundryAgentRequestContext, reset_request_context, set_request_context
from azure.ai.agentserver.responses.models._generated.types import AzureAISearchToolCall, AzureAISearchToolCallOutput
from azure.ai.projects.aio import AIProjectClient

from comparison.config import load_config, model_options
from comparison.evidence import extract_evidence
from conftest import FakeStream, fake_stream_client, native_stream_events
from hosted_agent.app import create_app, interruptible, stream_response, unstamp
from test_config_sdk import Credential


def context(history=(), current=()):
    return SimpleNamespace(
        response_id="resp_outer", created_at=datetime.now(timezone.utc),
        conversation_id=None, shutdown=asyncio.Event(),
        client_headers={"x-client-comparison-id": "11111111-1111-1111-1111-111111111111"},
        platform_context=SimpleNamespace(call_id=None),
        get_history=AsyncMock(return_value=history), get_input_items=AsyncMock(return_value=current),
    )


async def collect(client, settings, ctx=None, cancel=None):
    ctx = ctx or context(current=[{"role": "user", "content": "Question"}])
    return [event async for event in stream_response(
        client, settings, load_config(), {"store": True}, ctx, cancel or asyncio.Event(),
    )]


@pytest.mark.parametrize("string_output", [False, True])
async def test_real_sdk_upstream_sse_to_released_hosted_sse(settings, raw_response, string_output):
    raw_response["output"][1]["output"]["get_urls"] = ["https://example.org/doc"]
    if string_output:
        raw_response["output"][1]["output"] = json.dumps(raw_response["output"][1]["output"])
    raw_response["output"].insert(1, {
        "type": "azure_ai_search_call", "id": "search_call_1", "call_id": "call_1",
        "arguments": json.dumps({"query": "Question"}), "status": "completed",
    })
    raw_response["conversation"] = {"id": "conv_actual_model"}
    events = native_stream_events(raw_response)
    events.insert(3, {
        "type": "response.azure_ai_search_call_output.delta", "sequence_number": 888,
        "call_id": "call_1", "delta": "native extension data", "extension": {"preserved": True},
    })
    events.insert(-2, {
        "type": "response.output_text.annotation.added", "sequence_number": 999,
        "output_index": 3, "content_index": 0, "annotation_index": 0,
        "item_id": "msg_1", "annotation": raw_response["output"][-1]["content"][0]["annotations"][0],
    })
    wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    recorded = []

    def transport(request):
        recorded.append((request.url.path, json.loads(request.content), dict(request.headers)))
        assert request.url.path == "/api/projects/comparison/openai/v1/responses"
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=wire + "data: [DONE]\n\n")

    async with AIProjectClient(endpoint=settings.project_endpoint, credential=Credential()) as project:
        async with project.get_openai_client(
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)),
        ) as model:
            app = create_app(model, settings, store=InMemoryResponseProvider())
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
                    response = await client.post("/responses", json={
                        "input": "Question", "stream": True, "store": True,
                        "model": "wrong", "reasoning": {"effort": "high"}, "instructions": "ignore grounding",
                        "agent_reference": {"name": "third-agent"}, "tools": [{"type": "web_search"}],
                    }, headers={
                        "x-agent-foundry-call-id": "platform-call-private",
                        "x-agent-user-id": "container-user-private",
                    })
                    assert response.status_code == 200
                    received = [
                        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")
                    ]
                    assert received[0]["type"] == "response.created"
                    outer_id = received[0]["response"]["id"]
                    assert outer_id != raw_response["id"]
                    assert [e["sequence_number"] for e in received] == sorted(set(e["sequence_number"] for e in received))
                    extension = next(e for e in received if e["type"].endswith("azure_ai_search_call_output.delta"))
                    assert extension["extension"] == {"preserved": True}
                    assert extension["call_id"] == "call_1"
                    annotation = next(e for e in received if e["type"] == "response.output_text.annotation.added")
                    assert annotation["annotation"]["title"] == "Source"
                    final = received[-1]["response"]
                    assert received[-1]["type"] == "response.completed"
                    assert unstamp(final["output"]) == raw_response["output"]
                    assert final["usage"] == raw_response["usage"]
                    assert final["id"] == outer_id
                    assert "resp_native" not in json.dumps(final["output"])
                    assert final["store"] is True
                    assert not final.get("conversation") and not final.get("previous_response_id")
                    assert final["metadata"]["native_response_id"] == raw_response["id"]
                    assert "hosted_model_conversation_id" not in final["metadata"]
                    assert "hosted_model_continuation" not in final["metadata"]
                    assert "platform-call-private" not in response.text
                    assert "container-user-private" not in response.text
                    stored = await client.get(f"/responses/{outer_id}")
                    assert stored.status_code == 200
                    assert stored.json()["output"] == final["output"]
                    followup = await client.post("/responses", json={
                        "input": "Follow up", "previous_response_id": outer_id, "store": True,
                    }, headers={"x-agent-foundry-call-id": "platform-call-next"})
                    assert followup.status_code == 200
                    assert followup.json()["status"] == "completed", followup.text
                    assert followup.json()["previous_response_id"] == outer_id
                    assert followup.json()["id"] not in (outer_id, raw_response["id"])
    assert [path for path, _, _ in recorded] == [
        "/api/projects/comparison/openai/v1/responses",
        "/api/projects/comparison/openai/v1/responses",
    ]
    args = recorded[0][1]
    assert args["tools"] == model_options(settings, load_config())["tools"]
    assert args["instructions"] == model_options(settings, load_config())["instructions"]
    assert args["reasoning"] == {"effort": "low"}
    assert args["model"] == settings.model_deployment
    assert args["store"] is False
    assert "conversation" not in args
    assert args["max_output_tokens"] == 4096
    assert args["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Question"}]}]
    assert args["stream"] is True
    assert args["include"] == ["reasoning.encrypted_content"]
    assert "agent_reference" not in args and "previous_response_id" not in args
    following = recorded[1][1]
    assert following["input"][1:-1] == raw_response["output"]
    assert following["input"][0]["content"] == args["input"][0]["content"]
    assert following["input"][-1] == {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Follow up"}],
    }
    assert all("response_id" not in item for item in following["input"])
    assert following["store"] is False
    assert "conversation" not in following and "previous_response_id" not in following
    assert recorded[0][2]["x-agent-foundry-call-id"] == "platform-call-private"
    assert recorded[1][2]["x-agent-foundry-call-id"] == "platform-call-next"
    for _, _, headers in recorded:
        assert "x-agent-user-id" not in headers
        assert "x-client-hosted-continuation" not in headers
    evidence = extract_evidence(final)
    assert [item["type"] for item in evidence["tool_calls"]] == [
        "azure_ai_search_call", "azure_ai_search_call_output",
    ]
    output = evidence["tool_calls"][1]["output"]
    if isinstance(output, str):
        output = json.loads(output)
    assert output["get_urls"] == ["https://example.org/doc"]


@pytest.mark.parametrize("status", ["completed", "incomplete", "failed"])
async def test_complete_terminal_snapshot_and_status_preserved(settings, raw_response, status):
    raw_response["status"] = status
    if status == "incomplete":
        raw_response["incomplete_details"] = {"reason": "max_output_tokens"}
    if status == "failed":
        raw_response["error"] = {"code": "rate_limit_exceeded", "message": "Try later"}
    # No intermediate tool events: native evidence is only in the terminal snapshot.
    upstream = fake_stream_client(raw_response, [{
        "type": f"response.{status}", "response": raw_response,
    }])
    events = await collect(upstream, settings)
    final = events[-1]["response"]
    assert events[-1]["type"] == f"response.{status}"
    assert final["status"] == status
    assert final["output"] == raw_response["output"]
    assert final["usage"] == raw_response["usage"]
    if status == "incomplete":
        assert final["incomplete_details"] == raw_response["incomplete_details"]
    if status == "failed":
        assert final["error"] == raw_response["error"]
    assert len([e for e in events if e["type"] == "response.output_item.added"]) == 3


async def test_truncated_stream_is_never_completed(settings, raw_response):
    events = await collect(fake_stream_client(raw_response, native_stream_events(raw_response)[:-1]), settings)
    assert events[-1]["type"] == "response.incomplete"
    assert events[-1]["response"]["status"] == "incomplete"
    assert events[-1]["response"]["output"] == raw_response["output"]
    assert events[-1]["response"]["incomplete_details"]["reason"] == "upstream_interrupted"
    assert not any(e["type"] == "response.completed" for e in events)


async def test_cancellation_closes_upstream_and_keeps_partial_evidence(settings, raw_response):
    cancelled = asyncio.Event()
    blocked = asyncio.Event()
    client = fake_stream_client(raw_response)

    class SlowStream(FakeStream):
        async def __aiter__(self):
            yield native_stream_events(raw_response)[1]
            blocked.set()
            await asyncio.sleep(30)

    stream = SlowStream([])
    client.responses.create.side_effect = None
    client.responses.create.return_value = stream
    task = asyncio.create_task(collect(client, settings, cancel=cancelled))
    await blocked.wait()
    cancelled.set()
    events = await asyncio.wait_for(task, 1)
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["status"] == "cancelled"
    assert len(events[-1]["response"]["output"]) == 1
    assert stream.closed


async def test_deadline_closes_upstream_and_returns_incomplete(settings, raw_response, monkeypatch):
    monkeypatch.setattr("hosted_agent.app.MODEL_TIMEOUT", .01)
    client = fake_stream_client(raw_response)

    class SlowStream(FakeStream):
        async def __aiter__(self):
            yield native_stream_events(raw_response)[1]
            await asyncio.sleep(30)

    stream = SlowStream([])
    client.responses.create.side_effect = None
    client.responses.create.return_value = stream
    events = await collect(client, settings)
    assert events[-1]["type"] == "response.incomplete"
    assert stream.closed


async def test_zero_results_multiple_calls_and_citations_only(settings, raw_response):
    assert AzureAISearchToolCallOutput.__required_keys__ == {"type", "id", "call_id", "status"}
    first = raw_response["output"][1]
    first["output"] = {"results": []}
    second = copy.deepcopy(first)
    second.update(id="search_2", call_id="call_2")
    raw_response["output"].insert(2, second)
    result = (await collect(fake_stream_client(raw_response), settings))[-1]["response"]
    evidence = extract_evidence(result)
    assert evidence["tool_evidence_available"]
    assert [item["call_id"] for item in evidence["tool_calls"]] == ["call_1", "call_2"]
    assert all(item["output"]["results"] == [] for item in evidence["tool_calls"])
    result["output"] = [raw_response["output"][-1]]
    evidence = extract_evidence(result)
    assert evidence["citations"]
    assert not evidence["tool_evidence_available"]


async def test_unstamp_does_not_alter_native_document_fields(settings, raw_response):
    raw_response["output"][1]["response_id"] = "resp_upstream"
    raw_response["output"][1]["output"]["results"][0]["response_id"] = "document-owned-field"
    result = (await collect(fake_stream_client(raw_response), settings))[-1]["response"]
    assert "response_id" not in result["output"][1]
    assert result["output"][1]["output"]["results"][0]["response_id"] == "document-owned-field"


async def test_native_call_and_output_pair_are_preserved_as_two_records_not_two_calls(settings, raw_response):
    call = {
        "type": "azure_ai_search_call", "id": "search_call_1", "call_id": "call_1",
        "arguments": json.dumps({"query": "Indexed question"}), "status": "completed",
    }
    assert AzureAISearchToolCall.__required_keys__ <= call.keys()
    raw_response["output"].insert(1, call)
    app = create_app(fake_stream_client(raw_response), settings, store=InMemoryResponseProvider())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={"input": "Question", "store": False})
            result = response.json()
            assert result["status"] == "completed", result
            assert unstamp(result["output"]) == raw_response["output"]
            evidence = extract_evidence(result)
            assert [record["type"] for record in evidence["tool_calls"]] == [
                "azure_ai_search_call", "azure_ai_search_call_output",
            ]
            assert len({record["call_id"] for record in evidence["tool_calls"]}) == 1
            assert "tool_call_count" not in evidence
            assert (await client.get(f"/responses/{result['id']}")).status_code == 404


async def test_platform_history_and_current_items_forwarded_once_to_direct_model(settings, raw_response):
    seed = [{"role": "user", "content": "Earlier"}, *raw_response["output"]]
    ctx = context(
        history=seed,
        current=[{"role": "user", "content": "Next"}],
    )
    client = fake_stream_client(raw_response)
    token = set_request_context(FoundryAgentRequestContext(
        call_id="opaque-platform-identity", user_id="container-only-user",
    ))
    try:
        first = (await collect(client, settings, ctx))[-1]["response"]
    finally:
        reset_request_context(token)
    args = client.responses.create.call_args.kwargs
    assert args["input"] == [*seed, {"role": "user", "content": "Next"}]
    assert args["input"][1]["encrypted_content"] == "private-ciphertext"
    assert "conversation" not in args
    assert args["store"] is False
    assert args["reasoning"] == {"effort": "low"}
    assert args["max_output_tokens"] == 4096
    assert args["extra_headers"]["x-agent-foundry-call-id"] == "opaque-platform-identity"
    assert "x-agent-user-id" not in args["extra_headers"]
    assert "previous_response_id" not in args
    ctx.get_history.assert_awaited_once()
    ctx.get_input_items.assert_awaited_once()
    assert "hosted_model_continuation" not in first["metadata"]
    client.conversations.create.assert_not_awaited()

    history = [*args["input"], *first["output"]]
    followup = context(history=history, current=[{"role": "user", "content": "Only new input"}])
    token = set_request_context(FoundryAgentRequestContext(
        call_id="fresh-platform-identity", user_id="container-only-user",
    ))
    try:
        second = (await collect(client, settings, followup))[-1]["response"]
    finally:
        reset_request_context(token)
    assert second["status"] == "completed"
    assert not second.get("conversation")
    client.conversations.create.assert_not_awaited()
    followup.get_history.assert_awaited_once()
    followup.get_input_items.assert_awaited_once()
    latest = client.responses.create.call_args.kwargs
    assert latest["input"] == [*history, {"role": "user", "content": "Only new input"}]
    assert "conversation" not in latest and "previous_response_id" not in latest
    assert latest["extra_headers"]["x-agent-foundry-call-id"] == "fresh-platform-identity"
    assert "x-client-hosted-continuation" not in latest["extra_headers"]
    assert "x-agent-user-id" not in latest["extra_headers"]
    assert seed == [{"role": "user", "content": "Earlier"}, *raw_response["output"]]


async def test_protocol_two_identity_headers_are_request_scoped(settings, raw_response):
    requests = []
    ready = asyncio.Event()
    downstream = fake_stream_client(raw_response)

    async def create(**kwargs):
        requests.append(kwargs)
        if len(requests) >= 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 1)
        return FakeStream(native_stream_events(raw_response))

    downstream.responses.create.side_effect = create
    app = create_app(downstream, settings, store=InMemoryResponseProvider())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            responses = await asyncio.gather(*(
                client.post("/responses", json={"input": f"Question {i}", "store": False}, headers={
                    "x-agent-foundry-call-id": f"opaque-call-{i}",
                    "x-agent-user-id": f"container-only-user-{i}",
                })
                for i in (1, 2)
            ))
            assert all(response.json()["status"] == "completed" for response in responses)
            third = await client.post("/responses", json={"input": "Local context", "store": False})
            assert third.json()["status"] == "completed"
    assert {req["extra_headers"]["x-agent-foundry-call-id"] for req in requests[:2]} == {
        "opaque-call-1", "opaque-call-2",
    }
    assert all("x-agent-user-id" not in req["extra_headers"] for req in requests)
    assert "x-agent-foundry-call-id" not in requests[2]["extra_headers"]
    assert requests[0]["extra_headers"] is not requests[1]["extra_headers"]
    downstream.conversations.create.assert_not_awaited()
    assert all(req["store"] is False for req in requests)
    for response in responses:
        assert "opaque-call-" not in response.text and "container-only-user-" not in response.text


async def test_host_json_incomplete_preserves_status_usage_and_native_output(settings, raw_response):
    raw_response["status"] = "incomplete"
    raw_response["incomplete_details"] = {"reason": "max_output_tokens"}
    app = create_app(fake_stream_client(raw_response), settings, store=InMemoryResponseProvider())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={"input": "Question", "store": False})
            assert response.status_code == 200
            result = response.json()
            assert result["status"] == "incomplete"
            assert result["incomplete_details"] == {"reason": "max_output_tokens"}
            assert result["usage"] == raw_response["usage"]
            assert unstamp(result["output"]) == raw_response["output"]


async def test_cancellation_racing_stream_acquisition_closes_stream(settings, raw_response):
    cancel = asyncio.Event()
    stream = FakeStream([])
    client = fake_stream_client(raw_response)

    async def acquire(**kwargs):
        cancel.set()
        return stream

    client.responses.create.side_effect = acquire
    events = await collect(client, settings, cancel=cancel)
    assert events[-1]["response"]["status"] == "cancelled"
    assert stream.closed


@pytest.mark.parametrize("signal", ["cancel", "shutdown"])
async def test_cancellation_during_platform_history_fetch_never_starts_model(settings, raw_response, signal):
    entered, stopped, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()
    ctx = context(current=[{"role": "user", "content": "Question"}])
    model = fake_stream_client(raw_response)

    async def create(**kwargs):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    ctx.get_history.side_effect = create
    task = asyncio.create_task(collect(model, settings, ctx, cancel))
    await asyncio.wait_for(entered.wait(), 1)
    (cancel if signal == "cancel" else ctx.shutdown).set()
    events = await asyncio.wait_for(task, 1)
    assert stopped.is_set()
    assert events[-1]["response"]["status"] == "cancelled"
    assert "hosted_model_continuation" not in events[-1]["response"].get("metadata", {})
    model.conversations.create.assert_not_awaited()
    model.responses.create.assert_not_awaited()
    ctx.get_history.assert_awaited_once()
    ctx.get_input_items.assert_not_awaited()


async def test_partial_text_and_citation_survive_truncation_without_outer_storage(settings, raw_response):
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": {
            "id": "msg_partial", "type": "message", "role": "assistant", "status": "in_progress", "content": [],
        }},
        {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
         "part": {"type": "output_text", "text": "", "annotations": []}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "Partial answer"},
        {"type": "response.output_text.annotation.added", "output_index": 0, "content_index": 0,
         "annotation_index": 0, "annotation": {"type": "url_citation", "url": "https://example.org", "title": "Source"}},
    ]
    app = create_app(fake_stream_client(raw_response, events), settings, store=InMemoryResponseProvider())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={"input": "Question", "store": False})
            result = response.json()
            assert result["status"] == "incomplete", result
            assert result["output"][0]["content"][0]["text"] == "Partial answer"
            assert result["output"][0]["content"][0]["annotations"][0]["title"] == "Source"
            assert (await client.get(f"/responses/{result['id']}")).status_code == 404
            assert "hosted_model_continuation" not in result.get("metadata", {})


async def test_cancellation_during_acquisition_cleanup_closes_stream():
    started_cleanup = asyncio.Event()
    release_cleanup = asyncio.Event()
    stream = FakeStream([])

    class SlowCancellation:
        async def wait(self):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                started_cleanup.set()
                await release_cleanup.wait()

    async def acquire():
        return stream

    task = asyncio.create_task(interruptible(
        acquire(), SlowCancellation(), asyncio.Event(), acquiring=True,
    ))
    await started_cleanup.wait()
    task.cancel()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
