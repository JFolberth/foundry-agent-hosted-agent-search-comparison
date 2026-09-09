import asyncio
import copy
import json
import re
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from azure.ai.agentserver.core import FoundryAgentRequestContext, reset_request_context, set_request_context
from azure.ai.agentserver.responses import InMemoryResponseProvider
from azure.ai.projects.aio import AIProjectClient
from azure.monitor.opentelemetry.exporter.export.trace._exporter import _convert_span_to_envelope
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from comparison.config import load_config
from comparison.contracts import CompareRequest, MAX_TURNS
from comparison.service import ComparisonService
from comparison.state import HistoryStore
from conftest import fake_client, fake_stream_client, native_stream_events, FakeStream
from hosted_agent.app import create_app as create_hosted_app
from hosted_agent.app import stream_response
from test_config_sdk import Credential
from test_hosted_stream import context
from web.app import create_app


async def test_real_sdk_prompt_conversation_and_stateless_hosted_never_double_append(settings, raw_response):
    sent = []

    def transport(request):
        body = json.loads(request.content)
        sent.append((request.url.path, body, dict(request.headers)))
        side = "prompt" if settings.prompt_agent in request.url.path else "hosted"
        if request.url.path.endswith("/conversations"):
            return httpx2.Response(200, json={
                "id": f"conv_{side}", "object": "conversation", "created_at": 1, "metadata": {},
            })
        if side == "prompt":
            return httpx2.Response(200, json={
                **raw_response, "id": "resp_prompt", "conversation": {"id": body["conversation"]},
            })
        return httpx2.Response(200, json={
            **raw_response, "id": "resp_hosted", "store": False, "metadata": {
                "hosted_model_conversation_id": "conv_actual_model",
                "conversation_scope": "hosted_model",
                "hosted_model_continuation": ("a" if "x-client-hosted-continuation" not in request.headers else "b") * 43,
            },
        })

    async with AsyncExitStack() as stack:
        project = await stack.enter_async_context(AIProjectClient(
            endpoint=settings.project_endpoint, credential=Credential(),
        ))
        clients = {}
        for side, name in (("prompt", settings.prompt_agent), ("hosted", settings.hosted_agent)):
            clients[side] = await stack.enter_async_context(project.get_openai_client(
                agent_name=name, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)),
            ))
        service = ComparisonService(clients, load_config())
        seed = [{"role": "user", "content": "Earlier"}, {"role": "assistant", "content": "Prior answer"}]
        first = await service.compare(CompareRequest(
            message="First", history={"prompt": seed, "hosted": seed},
        ), "comparison-distinct")
        assert first["prompt"]["conversation_id"] == "conv_prompt"
        assert first["hosted"]["conversation_id"] == "conv_actual_model"
        assert first["prompt"]["conversation_scope"] == "agent"
        assert first["hosted"]["conversation_scope"] == "hosted_model"
        assert "inside the hosted container" in first["hosted"]["conversation_note"]
        assert first["hosted"]["continuation"] != "a" * 43
        private = service.store.get(first["hosted"]["continuation"], "hosted")
        assert private.provider_token == "a" * 43
        assert private.items == []
        assert "a" * 43 not in json.dumps(first)
        tokens = {side: first[side]["continuation"] for side in clients}
        second = await service.compare(CompareRequest(
            message="Next", continuation=tokens, history={"prompt": seed, "hosted": seed},
        ), "comparison-next")
        assert all(result["error"] is None for result in second.values())
        assert second["hosted"]["conversation_id"] == "conv_actual_model"
        assert second["hosted"]["continuation"] != first["hosted"]["continuation"]
        assert service.store.get(second["hosted"]["continuation"], "hosted").provider_token == "b" * 43
        assert "b" * 43 not in json.dumps(second)
    creates = [(path, body) for path, body, _ in sent if path.endswith("/conversations")]
    assert len(creates) == 1
    assert "/prompt-search/" in creates[0][0]
    assert all(body["items"] == seed for _, body in creates)
    for side in ("prompt", "hosted"):
        responses = [body for path, body, _ in sent if path.endswith("/responses") and f"/{side}-search/" in path]
        assert len(responses) == 2
        assert responses[0]["input"] == (seed if side == "hosted" else []) + [{"role": "user", "content": "First"}]
        assert responses[1]["input"] == [{"role": "user", "content": "Next"}]
        assert all("previous_response_id" not in body and body["store"] is (side == "prompt") for body in responses)
        if side == "prompt":
            assert all(body["conversation"] == "conv_prompt" for body in responses)
        else:
            assert all("conversation" not in body for body in responses)
    headers = [headers for path, _, headers in sent if path.endswith("/responses") and "/hosted-search/" in path]
    assert "x-client-hosted-continuation" not in headers[0]
    assert headers[1]["x-client-hosted-continuation"] == "a" * 43


async def test_unsupported_conversation_is_null_and_independent(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    clients["prompt"].conversations.create.side_effect = RuntimeError("secret=credential; 501 unsupported")
    result = await ComparisonService(clients, load_config()).compare(CompareRequest(message="Q"), "comparison")
    assert result["prompt"]["conversation_id"] is None
    assert "No provider" in result["prompt"]["conversation_note"]
    assert result["prompt"]["error"]
    clients["prompt"].responses.create.assert_not_awaited()
    assert result["hosted"]["conversation_id"].startswith("conv_hosted_model_")
    assert result["hosted"]["conversation_scope"] == "hosted_model"
    clients["hosted"].conversations.create.assert_not_awaited()
    assert result["hosted"]["error"] is None
    assert "credential" not in json.dumps(result)


@pytest.mark.parametrize("body", [
    {"message": "Q", "conversation_id": "conv_attacker"},
    {"message": "Q", "conversation": {"id": "conv_attacker"}},
    {"message": "Q", "continuation": {"prompt": {"conversation_id": "conv_attacker"}}},
])
def test_browser_cannot_supply_provider_conversations(body):
    with pytest.raises(ValueError):
        CompareRequest.model_validate(body)


@pytest.mark.parametrize("model_id", ["resp_model", None, {"id": "resp_untrusted"}])
async def test_http_ids_match_actual_exported_ui_and_runtime_spans(settings, raw_response, monkeypatch, model_id):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("comparison-test")
    for module in ("comparison.telemetry", "comparison.service", "hosted_agent.app", "web.app"):
        monkeypatch.setattr(f"{module}.tracer", tracer)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    model_response = copy.deepcopy(raw_response)
    model_response["id"] = model_id
    model = fake_stream_client(model_response)
    runtime = create_hosted_app(model, settings, store=InMemoryResponseProvider())
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    hosted_payloads = []

    async with runtime.router.lifespan_context(runtime):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime), base_url="http://host") as host:
            async def call_host(**kwargs):
                headers = kwargs.pop("extra_headers")
                response = await host.post("/responses", json=kwargs, headers=headers)
                response.raise_for_status()
                payload = response.json()
                hosted_payloads.append(payload)
                return SimpleNamespace(model_dump=lambda **kwargs: payload)
            clients["hosted"].responses.create.side_effect = call_host
            web = create_app(ComparisonService(clients, load_config()))
            async with web.router.lifespan_context(web):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://web") as browser:
                    response = await browser.post("/api/compare", json={"message": "Question"})
                    assert response.status_code == 200
                    result = response.json()
    spans = exporter.get_finished_spans()
    assert all(result[side]["error"] is None for side in clients), result
    by_id = {f"{span.context.span_id:016x}": span for span in spans}
    for side in ("prompt", "hosted"):
        data = result[side]
        span = by_id[data["span_id"]]
        assert span.name == f"agent.{side}"
        envelope = _convert_span_to_envelope(span)
        assert envelope.tags["ai.operation.id"] == data["trace_id"]
        assert envelope.data.base_data.id == data["span_id"]
        assert data["trace_id"] != result["comparison_id"]
        assert data["conversation_id"] == span.attributes["gen_ai.conversation.id"]
        assert data["response_id"]
        assert span.attributes["gen_ai.response.id"] == data["response_id"]
    hosted = result["hosted"]
    assert hosted["conversation_scope"] == "hosted_model"
    assert hosted["conversation_id"] == model.responses.create.call_args.kwargs["conversation"]
    outer = clients["hosted"].responses.create.call_args.kwargs
    assert outer["store"] is False and "conversation" not in outer and "previous_response_id" not in outer
    clients["hosted"].conversations.create.assert_not_awaited()
    runtime_span = by_id[hosted["hosted_runtime_span_id"]]
    assert runtime_span.name == "hosted.model"
    assert hosted["model_response_id"] == (model_id if isinstance(model_id, str) else None)
    if isinstance(model_id, str):
        assert runtime_span.attributes["gen_ai.response.id"] == model_id
        assert hosted["response_id"] != model_id
    else:
        assert "gen_ai.response.id" not in runtime_span.attributes
    assert runtime_span.attributes["gen_ai.conversation.id"] == hosted["conversation_id"]
    assert runtime_span.parent.span_id == by_id[hosted["span_id"]].context.span_id
    assert hosted["hosted_runtime_trace_id"] == hosted["trace_id"] == result["prompt"]["trace_id"]
    assert hosted["hosted_runtime_span_id"] != hosted["span_id"]
    assert _convert_span_to_envelope(runtime_span).data.base_data.id == hosted["hosted_runtime_span_id"]
    assert result["prompt"]["hosted_runtime_span_id"] is None
    assert result["prompt"]["model_response_id"] is None
    assert "private-ciphertext" not in json.dumps(result)
    assert hosted_payloads[0]["metadata"]["hosted_model_continuation"] not in json.dumps(result)
    assert hosted["continuation"] != hosted_payloads[0]["metadata"]["hosted_model_continuation"]
    assert "metadata" not in hosted
    provider.shutdown()


async def test_unrecorded_telemetry_is_null_not_comparison_id(raw_response):
    result = await ComparisonService(
        {side: fake_client(raw_response) for side in ("prompt", "hosted")}, load_config(),
    ).compare(CompareRequest(message="Q"), "not-a-telemetry-id")
    for side in result.values():
        assert side["trace_id"] is None and side["span_id"] is None
        assert "No recorded" in side["telemetry_note"]


async def test_failed_turn_retires_capability_to_prevent_concurrent_retries(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    first = await service.compare(CompareRequest(message="First"), "c1")
    token = first["prompt"]["continuation"]
    clients["prompt"].responses.create.side_effect = RuntimeError("Network failed after provider append")
    second = await service.compare(CompareRequest(message="Next", continuation={"prompt": token}), "c2")
    assert second["prompt"]["continuation"] is None
    count = clients["prompt"].responses.create.await_count
    third = await service.compare(CompareRequest(message="Retry", continuation={"prompt": token}), "c3")
    assert "already used" in third["prompt"]["error"]
    assert clients["prompt"].responses.create.await_count == count


async def test_three_hosted_conversation_turns_keep_native_context_once(settings, raw_response):
    calls = []
    model = fake_stream_client(raw_response)

    async def respond(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        raw = copy.deepcopy(raw_response)
        raw["id"] += str(len(calls))
        for item in raw["output"]:
            item["id"] += str(len(calls))
            if "call_id" in item:
                item["call_id"] += str(len(calls))
        return FakeStream(native_stream_events(raw))

    model.responses.create.side_effect = respond
    runtime = create_hosted_app(model, settings, store=InMemoryResponseProvider())
    seed = [{"role": "user", "content": "Earlier"}, {"role": "assistant", "content": "Prior answer"}]
    headers = {"x-agent-user-id": "one-user"}
    conversation_id = None
    tokens = []
    async with runtime.router.lifespan_context(runtime):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime), base_url="http://host") as host:
            for index, text in enumerate(("First", "Second", "Third")):
                headers["x-agent-foundry-call-id"] = f"platform-turn-{index}"
                response = await host.post("/responses", json={
                    "input": (seed if index == 0 else []) + [{"role": "user", "content": text}], "store": False,
                }, headers=headers)
                assert response.json()["status"] == "completed", response.text
                result = response.json()
                assert not result.get("conversation") and not result.get("previous_response_id")
                assert result["store"] is False
                metadata = result["metadata"]
                conversation_id = conversation_id or metadata["hosted_model_conversation_id"]
                assert metadata["hosted_model_conversation_id"] == conversation_id
                assert metadata["conversation_scope"] == "hosted_model"
                token = metadata["hosted_model_continuation"]
                assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
                assert token not in tokens
                tokens.append(token)
                headers["x-client-hosted-continuation"] = token
                assert (await host.get(f"/responses/{result['id']}")).status_code == 404
    model.conversations.create.assert_awaited_once()
    created = model.conversations.create.call_args.kwargs
    assert [item["content"][0]["text"] for item in created["items"]] == ["Earlier", "Prior answer"]
    assert created["extra_headers"]["x-agent-foundry-call-id"] == "platform-turn-0"
    assert "x-agent-user-id" not in created["extra_headers"]
    assert len(calls) == 3
    for index, (call, text) in enumerate(zip(calls, ("First", "Second", "Third"))):
        assert len(call["input"]) == 1
        assert call["input"][0]["content"] == [{"type": "input_text", "text": text}]
        assert call["conversation"] == conversation_id
        assert call["store"] is True and call["stream"] is True
        assert call["extra_headers"]["x-agent-foundry-call-id"] == f"platform-turn-{index}"
        assert "x-agent-user-id" not in call["extra_headers"]
        assert "x-client-hosted-continuation" not in call["extra_headers"]
        assert "previous_response_id" not in call
        assert "private-ciphertext" not in json.dumps(call)


async def test_mismatched_conversation_discards_other_conversation_content(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    first = await service.compare(CompareRequest(message="First"), "first")
    mismatched = {
        **raw_response, "conversation": {"id": "conv_someone_else"}, "metadata": {
            "hosted_model_conversation_id": "conv_someone_else",
            "hosted_model_continuation": "x" * 43, "conversation_scope": "hosted_model",
        },
    }
    for client in clients.values():
        client.responses.create.side_effect = None
        client.responses.create.return_value = SimpleNamespace(model_dump=lambda **kwargs: mismatched)
    result = await service.compare(CompareRequest(
        message="Q", continuation={side: first[side]["continuation"] for side in clients},
    ), "comparison")
    for side in result.values():
        assert "different conversation" in side["error"]
        assert side["text"] == ""
        assert side["citations"] == [] and side["tool_calls"] == []
        assert side["continuation"] is None
    assert "Indexed answer." not in json.dumps(result)


async def test_conversation_id_remains_visible_when_turn_limit_is_reached(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    token = service.store.put("prompt", [], 10, conversation_id="conv_provider_existing")
    result = await service.compare(CompareRequest(message="Too many turns", continuation={"prompt": token}), "c")
    assert result["prompt"]["conversation_id"] == "conv_provider_existing"
    assert "limit reached" in result["prompt"]["error"]
    clients["prompt"].responses.create.assert_not_awaited()
    clients["prompt"].conversations.create.assert_not_awaited()


async def hosted_turn(client, settings, store, *, token=None, message="Question"):
    ctx = context(current=[{"role": "user", "content": message}])
    if token:
        ctx.client_headers["x-client-hosted-continuation"] = token
    identity = set_request_context(FoundryAgentRequestContext(user_id="owner", call_id=f"call-{message}"))
    try:
        events = [event async for event in stream_response(
            client, settings, load_config(), {"store": False}, ctx, asyncio.Event(), store,
        )]
    finally:
        reset_request_context(identity)
    ctx.get_history.assert_not_awaited()
    return events[-1]["response"]


async def test_concurrent_hosted_token_claim_only_appends_one_new_turn(settings, raw_response):
    model = fake_stream_client(raw_response)
    store = HistoryStore()
    first = await hosted_turn(model, settings, store)
    token = first["metadata"]["hosted_model_continuation"]
    conversation_id = first["metadata"]["hosted_model_conversation_id"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return FakeStream(native_stream_events(raw_response))

    model.responses.create.side_effect = blocked
    winner = asyncio.create_task(hosted_turn(model, settings, store, token=token, message="Winner"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        loser = await asyncio.wait_for(hosted_turn(model, settings, store, token=token, message="Loser"), 1)
        assert loser["status"] == "failed"
        assert loser["error"]["code"] == "conversation_expired"
        assert conversation_id not in json.dumps(loser)
        assert token not in json.dumps(loser)
        assert model.responses.create.await_count == 2
    finally:
        release.set()
        result = await asyncio.wait_for(winner, 1)
    assert result["status"] == "completed"
    assert result["metadata"]["hosted_model_conversation_id"] == conversation_id
    assert result["metadata"]["hosted_model_continuation"] != token
    assert len(store._entries) == 1
    model.conversations.create.assert_awaited_once()
    latest = model.responses.create.call_args.kwargs
    assert latest["input"] == [{"role": "user", "content": "Winner"}]
    assert latest["conversation"] == conversation_id
    assert latest["extra_headers"]["x-agent-foundry-call-id"] == "call-Winner"


async def test_hosted_continuation_capacity_evicts_without_recreating_conversation(settings, raw_response):
    model = fake_stream_client(raw_response)
    store = HistoryStore(capacity=1)
    first = await hosted_turn(model, settings, store, message="First conversation")
    second = await hosted_turn(model, settings, store, message="Second conversation")
    assert len(store._entries) == 1
    rejected = await hosted_turn(model, settings, store, token=first["metadata"]["hosted_model_continuation"])
    assert rejected["status"] == "failed"
    assert rejected["error"]["code"] == "conversation_expired"
    assert model.conversations.create.await_count == model.responses.create.await_count == 2
    continued = await hosted_turn(model, settings, store, token=second["metadata"]["hosted_model_continuation"])
    assert continued["status"] == "completed"
    assert continued["metadata"]["hosted_model_conversation_id"] == second["metadata"]["hosted_model_conversation_id"]
    assert model.conversations.create.await_count == 2
    assert model.responses.create.await_count == 3
    assert len(store._entries) == 1
    assert all(entry.items == [] for entry in store._entries.values())


@pytest.mark.parametrize("failure", ["failed", "incomplete", "exception"])
async def test_failed_hosted_turn_retires_token_without_retrying_append(settings, raw_response, failure):
    model = fake_stream_client(raw_response)
    store = HistoryStore()
    first = await hosted_turn(model, settings, store)
    token = first["metadata"]["hosted_model_continuation"]
    if failure == "exception":
        model.responses.create.side_effect = RuntimeError("private failure after append")
    else:
        terminal = {**raw_response, "status": failure}
        model.responses.create.side_effect = None
        model.responses.create.return_value = FakeStream(native_stream_events(terminal))
    failed = await hosted_turn(model, settings, store, token=token)
    assert failed["status"] == ("failed" if failure == "exception" else failure)
    assert "hosted_model_continuation" not in failed.get("metadata", {})
    assert "private failure" not in json.dumps(failed)
    assert not store._entries
    retry = await hosted_turn(model, settings, store, token=token)
    assert retry["status"] == "failed"
    assert retry["error"]["code"] == "conversation_expired"
    model.conversations.create.assert_awaited_once()
    assert model.responses.create.await_count == 2


async def test_hosted_turn_cap_retires_token_before_model_append(settings, raw_response):
    model = fake_stream_client(raw_response)
    store = HistoryStore()
    token = store.put("hosted_model:owner", [], MAX_TURNS, conversation_id="conv_at_limit")
    result = await hosted_turn(model, settings, store, token=token)
    assert result["status"] == "incomplete"
    assert result["metadata"]["hosted_model_conversation_id"] == "conv_at_limit"
    assert result["metadata"]["conversation_scope"] == "hosted_model"
    assert "hosted_model_continuation" not in result["metadata"]
    assert not store._entries
    model.conversations.create.assert_not_awaited()
    model.responses.create.assert_not_awaited()
