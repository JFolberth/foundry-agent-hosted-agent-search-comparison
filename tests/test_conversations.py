import asyncio
import copy
import json
import re
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest
from azure.ai.agentserver.responses import InMemoryResponseProvider
from azure.ai.projects.aio import AIProjectClient
from azure.monitor.opentelemetry.exporter.export.trace._exporter import _convert_span_to_envelope
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from comparison.config import load_config
from comparison.contracts import CompareRequest, MAX_TURNS, MAX_HISTORY_CHARS, MAX_HISTORY_MESSAGES, MAX_MESSAGE
from comparison.evidence import extract_evidence
from comparison.service import ComparisonService
from comparison.state import HistoryStore
from conftest import fake_client, fake_stream_client, native_stream_events, FakeStream
from hosted_agent.app import create_app as create_hosted_app
from hosted_agent.app import stream_response
from test_config_sdk import Credential
from test_hosted_stream import context
from web.app import create_app


@pytest.mark.parametrize("conversation_id", [None, "conv_hosted_endpoint"])
async def test_real_sdk_prompt_conversation_and_stored_hosted_never_double_append(
    settings, raw_response, conversation_id,
):
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
            **raw_response, "id": f"resp_hosted_{len(sent)}", "store": True,
            "conversation": {"id": conversation_id} if conversation_id else None,
            "metadata": {
                "hosted_model_conversation_id": "conv_fabricated_model",
                "conversation_scope": "hosted_model",
                "hosted_model_continuation": "a" * 43,
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
        assert first["hosted"]["conversation_id"] == conversation_id
        assert first["prompt"]["conversation_scope"] == "agent"
        assert first["hosted"]["conversation_scope"] == "hosted_agent"
        assert ("platform-managed" if conversation_id else "stored response IDs") in first["hosted"]["conversation_note"]
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first["hosted"]["continuation"])
        assert first["hosted"]["continuation"] != "a" * 43
        private = service.store.get(first["hosted"]["continuation"], "hosted")
        assert private.response_id == first["hosted"]["response_id"]
        assert private.provider_token is None
        assert private.conversation_id == conversation_id
        assert private.items == []
        assert "a" * 43 not in json.dumps(first)
        assert "conv_fabricated_model" not in json.dumps(first)
        assert first["hosted"]["continuation"] != private.response_id
        tokens = {side: first[side]["continuation"] for side in clients}
        second = await service.compare(CompareRequest(
            message="Next", continuation=tokens, history={"prompt": seed, "hosted": seed},
        ), "comparison-next")
        assert all(result["error"] is None for result in second.values())
        assert second["hosted"]["conversation_id"] == conversation_id
        assert second["hosted"]["continuation"] != first["hosted"]["continuation"]
        assert service.store.get(second["hosted"]["continuation"], "hosted").response_id == second["hosted"]["response_id"]
        assert second["hosted"]["response_id"] != first["hosted"]["response_id"]
        assert second["prompt"]["conversation_id"] == first["prompt"]["conversation_id"]
    creates = [(path, body) for path, body, _ in sent if path.endswith("/conversations")]
    assert len(creates) == 1
    assert "/prompt-search/" in creates[0][0]
    assert all(body["items"] == seed for _, body in creates)
    for side in ("prompt", "hosted"):
        responses = [body for path, body, _ in sent if path.endswith("/responses") and f"/{side}-search/" in path]
        assert len(responses) == 2
        assert responses[0]["input"] == (seed if side == "hosted" else []) + [{"role": "user", "content": "First"}]
        assert responses[1]["input"] == [{"role": "user", "content": "Next"}]
        assert all(body["store"] is True for body in responses)
        if side == "prompt":
            assert all(body["conversation"] == "conv_prompt" for body in responses)
            assert all("previous_response_id" not in body for body in responses)
        else:
            assert all("conversation" not in body for body in responses)
            assert "previous_response_id" not in responses[0]
            assert responses[1]["previous_response_id"] == first["hosted"]["response_id"]
    headers = [headers for path, _, headers in sent if path.endswith("/responses") and "/hosted-search/" in path]
    assert all("x-client-hosted-continuation" not in header for header in headers)
    assert all(token not in json.dumps(sent) for token in tokens.values())


async def test_unsupported_conversation_is_null_and_independent(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    clients["prompt"].conversations.create.side_effect = RuntimeError("secret=credential; 501 unsupported")
    result = await ComparisonService(clients, load_config()).compare(CompareRequest(message="Q"), "comparison")
    assert result["prompt"]["conversation_id"] is None
    assert "No provider" in result["prompt"]["conversation_note"]
    assert result["prompt"]["error"]
    clients["prompt"].responses.create.assert_not_awaited()
    assert result["hosted"]["conversation_id"] is None
    assert result["hosted"]["conversation_scope"] == "hosted_agent"
    assert "stored response IDs" in result["hosted"]["conversation_note"]
    clients["hosted"].conversations.create.assert_not_awaited()
    assert result["hosted"]["error"] is None
    assert "credential" not in json.dumps(result)


@pytest.mark.parametrize("body", [
    {"message": "Q", "conversation_id": "conv_attacker"},
    {"message": "Q", "conversation": {"id": "conv_attacker"}},
    {"message": "Q", "previous_response_id": "resp_attacker"},
    {"message": "Q", "continuation": {"hosted": {"response_id": "resp_attacker"}}},
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
    model_response["conversation"] = {"id": "conv_native_not_hosted"}
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
        if data["conversation_id"] is None:
            assert "gen_ai.conversation.id" not in span.attributes
        else:
            assert data["conversation_id"] == span.attributes["gen_ai.conversation.id"]
        assert data["response_id"]
        assert span.attributes["gen_ai.response.id"] == data["response_id"]
    hosted = result["hosted"]
    assert hosted["conversation_scope"] == "hosted_agent"
    assert hosted["conversation_id"] is None
    assert "stored response IDs" in hosted["conversation_note"]
    inner = model.responses.create.call_args.kwargs
    assert inner["store"] is False and inner["stream"] is True
    assert "conversation" not in inner and "previous_response_id" not in inner
    model.conversations.create.assert_not_awaited()
    outer = clients["hosted"].responses.create.call_args.kwargs
    assert outer["store"] is True and "conversation" not in outer and "previous_response_id" not in outer
    clients["hosted"].conversations.create.assert_not_awaited()
    runtime_span = by_id[hosted["hosted_runtime_span_id"]]
    assert runtime_span.name == "hosted.model"
    assert hosted["model_response_id"] == (model_id if isinstance(model_id, str) else None)
    if isinstance(model_id, str):
        assert runtime_span.attributes["gen_ai.response.id"] == model_id
        assert hosted["response_id"] != model_id
    else:
        assert "gen_ai.response.id" not in runtime_span.attributes
    assert "gen_ai.conversation.id" not in runtime_span.attributes
    assert runtime_span.parent.span_id == by_id[hosted["span_id"]].context.span_id
    assert hosted["hosted_runtime_trace_id"] == hosted["trace_id"] == result["prompt"]["trace_id"]
    assert hosted["hosted_runtime_span_id"] != hosted["span_id"]
    assert _convert_span_to_envelope(runtime_span).data.base_data.id == hosted["hosted_runtime_span_id"]
    assert _convert_span_to_envelope(runtime_span).tags["ai.operation.id"] == hosted["hosted_runtime_trace_id"]
    assert result["prompt"]["hosted_runtime_span_id"] is None
    assert result["prompt"]["model_response_id"] is None
    assert "private-ciphertext" not in json.dumps(result)
    assert "conv_native_not_hosted" not in json.dumps(result)
    metadata = hosted_payloads[0]["metadata"]
    assert metadata.get("native_response_id") == (model_id if isinstance(model_id, str) else None)
    assert "hosted_model_continuation" not in metadata
    assert "hosted_model_conversation_id" not in metadata
    assert hosted["response_id"] == hosted_payloads[0]["id"]
    assert hosted["continuation"] != hosted["response_id"]
    for field in ("text", "citations", "tool_calls", "usage", "tool_evidence_available"):
        assert hosted[field] == result["prompt"][field] == extract_evidence(raw_response)[field]
    assert "metadata" not in hosted
    provider.shutdown()


async def test_unrecorded_telemetry_is_null_not_comparison_id(raw_response):
    result = await ComparisonService(
        {side: fake_client(raw_response) for side in ("prompt", "hosted")}, load_config(),
    ).compare(CompareRequest(message="Q"), "not-a-telemetry-id")
    for side in result.values():
        assert side["trace_id"] is None and side["span_id"] is None
        assert "No recorded" in side["telemetry_note"]


@pytest.mark.parametrize("side", ["prompt", "hosted"])
async def test_failed_turn_retires_capability_to_prevent_concurrent_retries(raw_response, side):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    first = await service.compare(CompareRequest(message="First"), "c1")
    token = first[side]["continuation"]
    clients[side].responses.create.side_effect = RuntimeError("Network failed after provider append")
    second = await service.compare(CompareRequest(message="Next", continuation={side: token}), "c2")
    assert second[side]["error"]
    assert second[side]["continuation"] is None
    count = clients[side].responses.create.await_count
    third = await service.compare(CompareRequest(message="Retry", continuation={side: token}), "c3")
    assert "already used" in third[side]["error"]
    assert clients[side].responses.create.await_count == count
    clients["hosted"].conversations.create.assert_not_awaited()


async def test_three_registered_hosted_turns_keep_platform_native_history_once(settings, raw_response):
    calls, native_outputs, outer_calls, results = [], [], [], []
    raw_response["output"].insert(1, {
        "type": "azure_ai_search_call", "id": "search_call_1", "call_id": "call_1",
        "arguments": json.dumps({"query": "What is indexed?"}), "status": "completed",
    })
    model = fake_stream_client(raw_response)

    async def respond(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        raw = copy.deepcopy(raw_response)
        raw["id"] += str(len(calls))
        for item in raw["output"]:
            item["id"] += str(len(calls))
            if "call_id" in item:
                item["call_id"] += str(len(calls))
            if item["type"] == "reasoning":
                item["encrypted_content"] += str(len(calls))
        native_outputs.append(copy.deepcopy(raw["output"]))
        return FakeStream(native_stream_events(raw))

    model.responses.create.side_effect = respond
    runtime = create_hosted_app(model, settings, store=InMemoryResponseProvider())
    seed = [{"role": "user", "content": "Earlier"}, {"role": "assistant", "content": "Prior answer"}]
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(runtime.router.lifespan_context(runtime))
        host = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runtime), base_url="http://host",
        ))

        async def transport(request):
            body = json.loads(request.content)
            outer_calls.append((request.url.path, body, dict(request.headers)))
            response = await host.post("/responses", json=body, headers={
                **dict(request.headers), "x-agent-user-id": "one-user",
                "x-agent-foundry-call-id": f"platform-turn-{len(outer_calls) - 1}",
            })
            return httpx2.Response(response.status_code, json=response.json())

        project = await stack.enter_async_context(AIProjectClient(
            endpoint=settings.project_endpoint, credential=Credential(),
        ))
        hosted = await stack.enter_async_context(project.get_openai_client(
            agent_name=settings.hosted_agent,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)),
        ))
        clients = {"prompt": fake_client(raw_response), "hosted": hosted}
        service = ComparisonService(clients, load_config())
        with pytest.MonkeyPatch.context() as patch:
            create_conversation = AsyncMock(side_effect=AssertionError("Hosted conversations must not be created"))
            patch.setattr(hosted.conversations, "create", create_conversation)
            for index, text in enumerate(("First", "Second", "Third")):
                continuation = {side: results[-1][side]["continuation"] for side in clients} if index else {}
                result = await service.compare(CompareRequest(
                    message=text, history={"prompt": seed, "hosted": seed}, continuation=continuation,
                ), f"comparison-turn-{index}")
                assert all(side["error"] is None for side in result.values()), result
                results.append(result)
                data = result["hosted"]
                assert data["conversation_id"] is None
                assert data["conversation_scope"] == "hosted_agent"
                assert "stored response IDs" in data["conversation_note"]
                assert data["model_response_id"] == raw_response["id"] + str(index + 1)
                private = service.store.get(data["continuation"], "hosted")
                assert private.response_id == data["response_id"]
                assert private.items == [] and private.provider_token is None
                assert private.turns == index + 2
                assert re.fullmatch(r"[A-Za-z0-9_-]{43}", data["continuation"])
                assert data["continuation"] != data["response_id"]
                assert "private-ciphertext" not in json.dumps(result)
                saved = await host.get(f"/responses/{data['response_id']}")
                assert saved.status_code == 200
                payload = saved.json()
                assert payload["store"] is True
                assert payload["id"] == data["response_id"]
                assert not payload.get("conversation")
                assert payload.get("previous_response_id") == (
                    results[index - 1]["hosted"]["response_id"] if index else None
                )
                assert payload["metadata"]["native_response_id"] == data["model_response_id"]
                assert "hosted_model_continuation" not in payload["metadata"]
                assert "hosted_model_conversation_id" not in payload["metadata"]
                for field in ("text", "citations", "tool_calls", "usage", "tool_evidence_available"):
                    assert data[field] == extract_evidence({"output": native_outputs[index], "usage": raw_response["usage"]})[field]
            create_conversation.assert_not_awaited()
    model.conversations.create.assert_not_awaited()
    clients["prompt"].conversations.create.assert_awaited_once()
    assert len(calls) == 3
    assert len({result["hosted"]["continuation"] for result in results}) == 3
    assert len({result["hosted"]["response_id"] for result in results}) == 3
    expected = []
    for index, (call, text) in enumerate(zip(calls, ("First", "Second", "Third"))):
        path, outer, headers = outer_calls[index]
        assert path == "/api/projects/comparison/agents/hosted-search/endpoint/protocols/openai/responses"
        assert outer["store"] is True and outer["stream"] is False
        assert outer["input"] == (seed if index == 0 else []) + [{"role": "user", "content": text}]
        assert "conversation" not in outer
        assert outer.get("previous_response_id") == (results[index - 1]["hosted"]["response_id"] if index else None)
        assert "x-client-hosted-continuation" not in headers
        assert all(result["hosted"]["continuation"] not in json.dumps(outer_calls) for result in results)
        if index == 0:
            assert [(item["role"], item["content"][0]["text"]) for item in call["input"][:2]] == [
                ("user", "Earlier"), ("assistant", "Prior answer"),
            ]
            expected.extend(copy.deepcopy(call["input"][:2]))
        current = call["input"][-1]
        assert current["role"] == "user"
        assert current["content"] == [{"type": "input_text", "text": text}]
        # The SDK assigns IDs and completed status when persisting inline input.
        for actual, prior in zip(call["input"], expected):
            if index and prior.get("type") == "message" and "id" not in prior:
                assert actual["id"].startswith("msg_")
                assert actual["status"] == "completed"
                prior.update(id=actual["id"], status="completed")
        expected.append(copy.deepcopy(current))
        assert call["input"] == expected
        expected.extend(native_outputs[index])
        assert call["store"] is False and call["stream"] is True
        assert call["include"] == ["reasoning.encrypted_content"]
        assert call["extra_headers"]["x-agent-foundry-call-id"] == f"platform-turn-{index}"
        assert "x-agent-user-id" not in call["extra_headers"]
        assert "x-client-hosted-continuation" not in call["extra_headers"]
        assert "conversation" not in call and "previous_response_id" not in call
        assert all("response_id" not in item and "agent_reference" not in item for item in call["input"])
        for prior in range(index):
            assert sum(item.get("encrypted_content") == f"private-ciphertext{prior + 1}" for item in call["input"]) == 1
            for output in native_outputs[prior]:
                assert call["input"].count(output) == 1


async def test_mismatched_conversation_discards_other_conversation_content(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    clients["hosted"] = fake_client({**raw_response, "conversation": {"id": "conv_original_hosted"}})
    service = ComparisonService(clients, load_config())
    first = await service.compare(CompareRequest(message="First"), "first")
    assert first["hosted"]["conversation_id"] == "conv_original_hosted"
    mismatched = {
        **raw_response, "conversation": {"id": "conv_someone_else"},
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
    clients["hosted"].conversations.create.assert_not_awaited()


@pytest.mark.parametrize("response_id", [None, {"id": "resp_untrusted"}, "invalid response id"])
async def test_ui_unusable_stored_response_id_retires_hosted_capability(raw_response, response_id):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    first = await service.compare(CompareRequest(message="First"), "first")
    token = first["hosted"]["continuation"]
    clients["hosted"].responses.create.side_effect = None
    clients["hosted"].responses.create.return_value = SimpleNamespace(
        model_dump=lambda **kwargs: {
            **raw_response, "id": response_id,
            "metadata": {"hosted_model_continuation": "a" * 43, "native_response_id": "resp_model_only"},
        },
    )
    result = await service.compare(CompareRequest(message="Next", continuation={"hosted": token}), "next")
    assert result["hosted"]["response_id"] is None
    assert result["hosted"]["model_response_id"] == "resp_model_only"
    assert result["hosted"]["error"]
    assert result["hosted"]["continuation"] is None
    assert "a" * 43 not in json.dumps(result)
    assert not any(entry.side == "hosted" for entry in service.store._entries.values())
    retry = await service.compare(CompareRequest(message="Retry", continuation={"hosted": token}), "retry")
    assert "already used" in retry["hosted"]["error"]
    assert clients["hosted"].responses.create.await_count == 2
    clients["hosted"].conversations.create.assert_not_awaited()


@pytest.mark.parametrize("side", ["prompt", "hosted"])
async def test_conversation_id_remains_visible_when_turn_limit_is_reached(raw_response, side):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    token = service.store.put(
        side, [], MAX_TURNS, conversation_id="conv_provider_existing",
        response_id="resp_existing" if side == "hosted" else None,
    )
    result = await service.compare(CompareRequest(message="Too many turns", continuation={side: token}), "c")
    assert result[side]["conversation_id"] == "conv_provider_existing"
    assert "limit reached" in result[side]["error"]
    assert result[side]["continuation"] is None
    assert token not in service.store._entries
    clients[side].responses.create.assert_not_awaited()
    clients[side].conversations.create.assert_not_awaited()


async def test_concurrent_ui_hosted_capability_only_invokes_one_followup(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    first = await service.compare(CompareRequest(message="First"), "first")
    token = first["hosted"]["continuation"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(model_dump=lambda **kwargs: {**raw_response, "id": "resp_winner"})

    clients["hosted"].responses.create.side_effect = blocked
    winner = asyncio.create_task(service.compare(
        CompareRequest(message="Winner", continuation={"hosted": token}), "winner",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        loser = await asyncio.wait_for(service.compare(
            CompareRequest(message="Loser", continuation={"hosted": token}), "loser",
        ), 1)
        assert "already used" in loser["hosted"]["error"]
        assert loser["hosted"]["continuation"] is None
        assert token not in json.dumps(loser)
        assert clients["hosted"].responses.create.await_count == 2
    finally:
        release.set()
        result = await asyncio.wait_for(winner, 1)
    assert result["hosted"]["error"] is None
    assert result["hosted"]["continuation"] != token
    assert service.store.get(result["hosted"]["continuation"], "hosted").response_id == "resp_winner"
    assert sum(entry.side == "hosted" for entry in service.store._entries.values()) == 1
    clients["hosted"].conversations.create.assert_not_awaited()
    latest = clients["hosted"].responses.create.call_args.kwargs
    assert latest["input"] == [{"role": "user", "content": "Winner"}]
    assert latest["previous_response_id"] == first["hosted"]["response_id"]
    assert "conversation" not in latest
    assert latest["store"] is True


async def test_ui_capacity_evicts_old_capabilities_without_recreating_history(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    store = HistoryStore(capacity=2)
    service = ComparisonService(clients, load_config(), store=store)
    first = await service.compare(CompareRequest(message="First conversation"), "first")
    second = await service.compare(CompareRequest(message="Second conversation"), "second")
    assert len(store._entries) == 2
    rejected = await service.compare(CompareRequest(
        message="Evicted", continuation={side: first[side]["continuation"] for side in clients},
    ), "evicted")
    for side in clients:
        assert "expired" in rejected[side]["error"]
        assert rejected[side]["continuation"] is None
        assert clients[side].responses.create.await_count == 2
    continued = await service.compare(CompareRequest(
        message="Continue", continuation={side: second[side]["continuation"] for side in clients},
    ), "continue")
    assert all(result["error"] is None for result in continued.values())
    assert clients["hosted"].responses.create.call_args.kwargs["previous_response_id"] == second["hosted"]["response_id"]
    assert clients["prompt"].responses.create.call_args.kwargs["conversation"] == second["prompt"]["conversation_id"]
    assert clients["prompt"].conversations.create.await_count == 2
    clients["hosted"].conversations.create.assert_not_awaited()
    assert all(client.responses.create.await_count == 3 for client in clients.values())
    assert len(store._entries) == 2
    assert all(entry.items == [] for entry in store._entries.values())
    assert all(entry.provider_token is None for entry in store._entries.values())


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled"])
async def test_ui_failed_hosted_response_retires_capability(raw_response, status):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    first = await service.compare(CompareRequest(message="First"), "first")
    token = first["hosted"]["continuation"]
    clients["hosted"].responses.create.side_effect = None
    clients["hosted"].responses.create.return_value = SimpleNamespace(
        model_dump=lambda **kwargs: {**raw_response, "status": status},
    )
    failed = await service.compare(CompareRequest(message="Next", continuation={"hosted": token}), "failed")
    assert failed["hosted"]["error"]
    assert failed["hosted"]["continuation"] is None
    assert not any(entry.side == "hosted" for entry in service.store._entries.values())
    retry = await service.compare(CompareRequest(message="Retry", continuation={"hosted": token}), "retry")
    assert "already used" in retry["hosted"]["error"]
    assert retry["hosted"]["continuation"] is None
    clients["hosted"].conversations.create.assert_not_awaited()
    assert clients["hosted"].responses.create.await_count == 2


@pytest.mark.parametrize("side", ["prompt", "hosted"])
@pytest.mark.parametrize("kind", ["missing", "expired", "wrong_side", "missing_reference"])
async def test_ui_invalid_capabilities_never_invoke_provider(raw_response, side, kind):
    clients = {name: fake_client(raw_response) for name in ("prompt", "hosted")}
    now = [0]
    store = HistoryStore(ttl=10, clock=lambda: now[0])
    service = ComparisonService(clients, load_config(), store=store)
    first = await service.compare(CompareRequest(message="First"), "first")
    token = first[side]["continuation"]
    if kind == "missing":
        token = "x" * 43
    elif kind == "expired":
        now[0] = 10
    elif kind == "wrong_side":
        token = first["hosted" if side == "prompt" else "prompt"]["continuation"]
    else:
        token = store.put(side, [], 1)
    count = clients[side].responses.create.await_count
    conversations = clients[side].conversations.create.await_count
    result = await service.compare(CompareRequest(
        message="Rejected", continuation={side: token},
        history={side: [{"role": "user", "content": "Must not silently restart"}]},
    ), "rejected")
    assert "expired" in result[side]["error"]
    assert result[side]["continuation"] is None
    assert result[side]["text"] == ""
    assert clients[side].responses.create.await_count == count
    assert clients[side].conversations.create.await_count == conversations
    clients["hosted"].conversations.create.assert_not_awaited()
    if kind == "wrong_side":
        owner = "hosted" if side == "prompt" else "prompt"
        assert store.get(token, owner)


async def test_ui_final_allowed_turn_issues_capability_but_next_turn_is_rejected(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    seed = [{"role": "user", "content": f"Earlier {index}"} for index in range(MAX_TURNS - 1)]
    final = await service.compare(CompareRequest(message="Last allowed", history={
        side: seed for side in clients
    }), "last")
    for side in clients:
        assert final[side]["error"] is None
        assert service.store.get(final[side]["continuation"], side).turns == MAX_TURNS
    rejected = await service.compare(CompareRequest(
        message="Over limit", continuation={side: final[side]["continuation"] for side in clients},
    ), "over")
    for side in clients:
        assert "limit reached" in rejected[side]["error"]
        assert rejected[side]["continuation"] is None
        assert clients[side].responses.create.await_count == 1
    assert not service.store._entries
    clients["hosted"].conversations.create.assert_not_awaited()


@pytest.mark.parametrize("body", [
    {"message": "x" * (MAX_MESSAGE + 1)},
    {"message": "Q", "history": {"hosted": [{"role": "user", "content": "Q"}] * (MAX_HISTORY_MESSAGES + 1)}},
    {"message": "Q", "history": {"hosted": [{"role": "assistant", "content": "x" * 12000}] * (MAX_HISTORY_CHARS // 12000 + 1)}},
])
async def test_ui_request_bounds_reject_before_service_invocation(raw_response, body):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    with pytest.raises(ValueError):
        await service.compare(CompareRequest.model_validate(body), "invalid")
    for client in clients.values():
        client.responses.create.assert_not_awaited()
        client.conversations.create.assert_not_awaited()
    assert not service.store._entries


async def test_stream_uses_native_context_history_and_current_input_without_mutating(settings, raw_response):
    history = copy.deepcopy(raw_response["output"])
    for item in history:
        item.update(response_id="resp_previous_hosted", agent_reference={"name": "hosted-search"})
    current = [{"role": "user", "content": "Next"}]
    original = copy.deepcopy(history)
    ctx = context(history=history, current=current)
    model = fake_stream_client(raw_response)
    events = [event async for event in stream_response(
        model, settings, load_config(), {"store": True, "previous_response_id": "resp_previous_hosted"},
        ctx, asyncio.Event(),
    )]
    result = events[-1]["response"]
    assert result["status"] == "completed"
    assert result["store"] is True
    assert result["previous_response_id"] == "resp_previous_hosted"
    assert not result.get("conversation")
    assert result["metadata"]["native_response_id"] == raw_response["id"]
    ctx.get_history.assert_awaited_once_with()
    ctx.get_input_items.assert_awaited_once_with()
    assert history == original
    assert current == [{"role": "user", "content": "Next"}]
    call = model.responses.create.call_args.kwargs
    assert call["input"] == raw_response["output"] + current
    assert call["store"] is False
    assert "conversation" not in call and "previous_response_id" not in call
    model.conversations.create.assert_not_awaited()


@pytest.mark.parametrize("prior", ["missing", "deleted"])
async def test_sdk_missing_or_deleted_prior_response_fails_without_model_call(settings, raw_response, prior):
    store = InMemoryResponseProvider()
    model = fake_stream_client(raw_response)
    runtime = create_hosted_app(model, settings, store=store)
    async with runtime.router.lifespan_context(runtime):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime), base_url="http://host") as host:
            first = await host.post("/responses", json={"input": "Prior history", "store": prior == "deleted"})
            assert first.status_code == 200 and first.json()["status"] == "completed"
            previous_id = first.json()["id"]
            if prior == "deleted":
                await store.delete_response(previous_id)
            assert (await host.get(f"/responses/{previous_id}")).status_code == 404
            model.responses.create.reset_mock()
            response = await host.post("/responses", json={
                "input": "Current only", "store": True, "previous_response_id": previous_id,
            })
    assert response.status_code == 404 or response.json().get("status") == "failed"
    model.responses.create.assert_not_awaited()
    model.conversations.create.assert_not_awaited()


async def test_explicit_platform_history_provider_failure_does_not_call_model(settings, raw_response, monkeypatch):
    store = InMemoryResponseProvider()
    model = fake_stream_client(raw_response)
    runtime = create_hosted_app(model, settings, store=store)
    async with runtime.router.lifespan_context(runtime):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime), base_url="http://host") as host:
            first = await host.post("/responses", json={"input": "First", "store": True})
            assert first.status_code == 200 and first.json()["status"] == "completed"
            lookup = AsyncMock(side_effect=RuntimeError("private provider history unavailable"))
            monkeypatch.setattr(store, "get_items", lookup)
            second = await host.post("/responses", json={
                "input": "Next", "store": True, "previous_response_id": first.json()["id"],
            })
    assert second.status_code == 200
    result = second.json()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "server_error"
    assert "private provider" not in json.dumps(result)
    assert result["output"] == []
    lookup.assert_awaited_once()
    model.responses.create.assert_awaited_once()
    model.conversations.create.assert_not_awaited()
