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
from azure.ai.agentserver.responses import InMemoryResponseProvider, ResponseContext
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
async def test_real_sdk_prompt_conversation_and_stateless_hosted_transcript(
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
            **raw_response, "id": f"resp_hosted_{len(sent)}", "store": False,
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
        assert ("Provider-reported" if conversation_id else "not stored") in first["hosted"]["conversation_note"]
        assert first["hosted"]["continuation"] is None
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first["prompt"]["continuation"])
        private = service.store.get(first["prompt"]["continuation"], "prompt")
        assert private.conversation_id == "conv_prompt"
        assert private.items == [] and private.response_id is None and private.provider_token is None
        assert private.turns == 2
        assert all(entry.side == "prompt" for entry in service.store._entries.values())
        assert "a" * 43 not in json.dumps(first)
        assert "conv_fabricated_model" not in json.dumps(first)
        tokens = {side: first[side]["continuation"] for side in clients}
        transcript = seed + [
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": first["hosted"]["text"]},
        ]
        second = await service.compare(CompareRequest(
            message="Next", continuation=tokens, history={"prompt": seed, "hosted": transcript},
        ), "comparison-next")
        assert all(result["error"] is None for result in second.values())
        assert second["hosted"]["conversation_id"] == conversation_id
        assert second["hosted"]["continuation"] is None
        assert second["hosted"]["response_id"] != first["hosted"]["response_id"]
        assert second["prompt"]["conversation_id"] == first["prompt"]["conversation_id"]
        assert second["prompt"]["continuation"] != first["prompt"]["continuation"]
        assert len(service.store._entries) == 1
        assert service.store.get(second["prompt"]["continuation"], "prompt").turns == 3
    creates = [(path, body) for path, body, _ in sent if path.endswith("/conversations")]
    assert len(creates) == 1
    assert "/prompt-search/" in creates[0][0]
    assert all(body["items"] == seed for _, body in creates)
    for side in ("prompt", "hosted"):
        responses = [body for path, body, _ in sent if path.endswith("/responses") and f"/{side}-search/" in path]
        assert len(responses) == 2
        assert responses[0]["input"] == (seed if side == "hosted" else []) + [{"role": "user", "content": "First"}]
        assert responses[1]["input"] == (transcript if side == "hosted" else []) + [{"role": "user", "content": "Next"}]
        assert all(body["store"] is (side == "prompt") for body in responses)
        assert all("previous_response_id" not in body for body in responses)
        if side == "prompt":
            assert all(body["conversation"] == "conv_prompt" for body in responses)
            assert all("previous_response_id" not in body for body in responses)
        else:
            assert all("conversation" not in body for body in responses)
    headers = [headers for path, _, headers in sent if path.endswith("/responses") and "/hosted-search/" in path]
    assert all("x-client-hosted-continuation" not in header for header in headers)
    assert all(token not in json.dumps(sent) for token in tokens.values() if token)


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
    assert "not stored" in result["hosted"]["conversation_note"]
    assert result["hosted"]["continuation"] is None
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
    assert "not stored" in hosted["conversation_note"]
    inner = model.responses.create.call_args.kwargs
    assert inner["store"] is False and inner["stream"] is True
    assert "conversation" not in inner and "previous_response_id" not in inner
    model.conversations.create.assert_not_awaited()
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
    assert hosted["continuation"] is None
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


async def test_failed_prompt_turn_retires_capability_to_prevent_concurrent_retries(raw_response):
    side = "prompt"
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


@pytest.mark.parametrize("native_evidence", [True, False])
async def test_three_browser_posts_resend_transcript_without_stored_get_or_sdk_history(
    settings, raw_response, monkeypatch, native_evidence,
):
    calls, native_outputs, outer_calls, results, transcripts = [], [], [], [], []
    raw_response["output"].insert(1, {
        "type": "azure_ai_search_call", "id": "search_call_1", "call_id": "call_1",
        "arguments": json.dumps({"query": "What is indexed?"}), "status": "completed",
    })
    if not native_evidence:
        raw_response["output"] = [
            item for item in raw_response["output"] if item["type"] in ("message", "reasoning")
        ]
        raw_response["usage"] = None
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
    store = InMemoryResponseProvider()
    history_reads = {}
    for owner, name in (
        (ResponseContext, "get_history"), (store, "get_history_item_ids"), (store, "get_items"),
    ):
        history_reads[name] = AsyncMock(side_effect=AssertionError("Stateless requests must not read history"))
        monkeypatch.setattr(owner, name, history_reads[name])
    runtime = create_hosted_app(model, settings, store=store)
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
        web = create_app(service)
        await stack.enter_async_context(web.router.lifespan_context(web))
        browser = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web), base_url="http://web",
        ))
        transcript = copy.deepcopy(seed)
        with pytest.MonkeyPatch.context() as patch:
            create_conversation = AsyncMock(side_effect=AssertionError("Hosted conversations must not be created"))
            patch.setattr(hosted.conversations, "create", create_conversation)
            for index, text in enumerate(("First", "Second", "Third")):
                continuation = {"prompt": results[-1]["prompt"]["continuation"]} if index else {}
                transcripts.append(copy.deepcopy(transcript))
                response = await browser.post("/api/compare", json={
                    "message": text, "history": {"prompt": seed, "hosted": transcript},
                    "continuation": continuation,
                })
                assert response.status_code == 200
                result = response.json()
                assert all(result[side]["error"] is None for side in clients), result
                results.append(result)
                data = result["hosted"]
                assert data["conversation_id"] is None
                assert data["conversation_scope"] == "hosted_agent"
                assert "not stored" in data["conversation_note"]
                assert data["model_response_id"] == raw_response["id"] + str(index + 1)
                assert data["continuation"] is None
                assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", data["response_id"])
                assert len(service.store._entries) == 1
                assert all(entry.side == "prompt" for entry in service.store._entries.values())
                assert "private-ciphertext" not in json.dumps(result)
                saved = await host.get(f"/responses/{data['response_id']}")
                assert saved.status_code == 404
                for field in ("text", "citations", "tool_calls", "usage", "tool_evidence_available"):
                    assert data[field] == extract_evidence({"output": native_outputs[index], "usage": raw_response["usage"]})[field]
                if not native_evidence:
                    assert data["tool_calls"] == []
                    assert data["tool_evidence_available"] is False
                    assert "does not mean zero calls" in data["tool_evidence_note"]
                    assert data["usage"] is None
                    assert data["citations"]
                transcript.extend([
                    {"role": "user", "content": text}, {"role": "assistant", "content": data["text"]},
                ])
            create_conversation.assert_not_awaited()
    for lookup in history_reads.values():
        lookup.assert_not_awaited()
    model.conversations.create.assert_not_awaited()
    clients["prompt"].conversations.create.assert_awaited_once()
    assert len(calls) == 3
    assert len({result["hosted"]["response_id"] for result in results}) == 3
    assert "private-ciphertext" not in json.dumps(calls)
    assert "private-ciphertext" not in json.dumps(outer_calls)
    for index, (call, text) in enumerate(zip(calls, ("First", "Second", "Third"))):
        path, outer, headers = outer_calls[index]
        assert path == "/api/projects/comparison/agents/hosted-search/endpoint/protocols/openai/responses"
        assert outer["store"] is False and outer["stream"] is False
        expected = transcripts[index] + [{"role": "user", "content": text}]
        assert outer["input"] == expected
        assert "conversation" not in outer and "previous_response_id" not in outer
        assert "x-client-hosted-continuation" not in headers
        assert len(call["input"]) == len(expected)
        for actual, message in zip(call["input"], expected):
            assert actual.get("type", "message") == "message"
            assert actual["role"] == message["role"]
            assert actual["content"] == [{
                "type": "output_text" if message["role"] == "assistant" else "input_text",
                "text": message["content"],
            }]
        assert call["store"] is False and call["stream"] is True
        assert call["include"] == ["reasoning.encrypted_content"]
        assert call["extra_headers"]["x-agent-foundry-call-id"] == f"platform-turn-{index}"
        assert "x-agent-user-id" not in call["extra_headers"]
        assert "x-client-hosted-continuation" not in call["extra_headers"]
        assert "conversation" not in call and "previous_response_id" not in call
        assert all("response_id" not in item and "agent_reference" not in item for item in call["input"])
        assert all(result["prompt"]["continuation"] not in json.dumps(outer_calls) for result in results)


async def test_prompt_conversation_stays_bound_but_hosted_reports_each_actual_conversation(raw_response):
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
    prompt = result["prompt"]
    assert "different conversation" in prompt["error"]
    assert prompt["text"] == ""
    assert prompt["citations"] == [] and prompt["tool_calls"] == []
    assert prompt["continuation"] is None
    assert "Indexed answer." not in json.dumps(prompt)
    hosted = result["hosted"]
    assert hosted["error"] is None
    assert hosted["conversation_id"] == "conv_someone_else"
    assert hosted["continuation"] is None
    assert hosted["text"] == "Indexed answer."
    assert "Provider-reported" in hosted["conversation_note"]
    clients["hosted"].responses.create.return_value = SimpleNamespace(model_dump=lambda **kwargs: raw_response)
    without_id = await service.compare(CompareRequest(message="No provider conversation"), "without-id")
    assert without_id["hosted"]["conversation_id"] is None
    assert "no conversation ID was reported" in without_id["hosted"]["conversation_note"]
    clients["hosted"].conversations.create.assert_not_awaited()
    assert all("conversation" not in call.kwargs for call in clients["hosted"].responses.create.await_args_list)


@pytest.mark.parametrize("reported", [
    None, "conv_actual", {"id": "conv_actual"}, "invalid conversation", {"id": "c" * 201},
])
async def test_hosted_conversation_id_is_only_a_valid_current_provider_report(raw_response, reported):
    raw = {
        **raw_response, "conversation": reported,
        "metadata": {"hosted_model_conversation_id": "conv_fabricated", "native_response_id": "resp_model"},
    }
    client = fake_client(raw)
    service = ComparisonService({"hosted": client}, load_config())
    result = await service._one("hosted", CompareRequest(
        message="Question", continuation={"hosted": "x" * 43},
    ), "provider-id")
    valid = reported == "conv_actual" or reported == {"id": "conv_actual"}
    assert result["conversation_id"] == ("conv_actual" if valid else None)
    assert ("Provider-reported" if valid else "no conversation ID was reported") in result["conversation_note"]
    assert result["continuation"] is None
    assert "conv_fabricated" not in json.dumps(result)
    assert result["response_id"] == raw_response["id"]
    if valid or reported is None:
        assert result["error"] is None
        assert result["model_response_id"] == "resp_model"
    client.conversations.create.assert_not_awaited()
    assert not service.store._entries


@pytest.mark.parametrize("response_id", [None, "", {"id": "resp_untrusted"}, "invalid response id", "r" * 181])
async def test_ui_requires_actual_valid_hosted_response_id_not_metadata_fallback(raw_response, response_id):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    first = await service.compare(CompareRequest(message="First"), "first")
    token = "obsolete_" + "a" * 40
    transcript = [
        {"role": "user", "content": "First"}, {"role": "assistant", "content": first["hosted"]["text"]},
    ]
    clients["hosted"].responses.create.side_effect = None
    clients["hosted"].responses.create.return_value = SimpleNamespace(
        model_dump=lambda **kwargs: {
            **raw_response, "id": response_id,
            "metadata": {"hosted_model_continuation": "a" * 43, "native_response_id": "resp_model_only"},
        },
    )
    result = await service.compare(CompareRequest(
        message="Next", continuation={"hosted": token}, history={"hosted": transcript},
    ), "next")
    assert result["hosted"]["response_id"] is None
    assert result["hosted"]["model_response_id"] == "resp_model_only"
    assert result["hosted"]["error"]
    assert result["hosted"]["continuation"] is None
    assert "a" * 43 not in json.dumps(result)
    assert not any(entry.side == "hosted" for entry in service.store._entries.values())
    clients["hosted"].responses.create.return_value = SimpleNamespace(model_dump=lambda **kwargs: raw_response)
    retry = await service.compare(CompareRequest(
        message="Retry", continuation={"hosted": token}, history={"hosted": transcript},
    ), "retry")
    assert retry["hosted"]["error"] is None
    assert retry["hosted"]["response_id"] == raw_response["id"]
    assert retry["hosted"]["continuation"] is None
    assert clients["hosted"].responses.create.await_count == 3
    assert clients["hosted"].responses.create.call_args.kwargs["input"] == transcript + [
        {"role": "user", "content": "Retry"},
    ]
    clients["hosted"].conversations.create.assert_not_awaited()


async def test_prompt_conversation_id_remains_visible_when_turn_limit_is_reached(raw_response):
    side = "prompt"
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    token = service.store.put(
        side, [], MAX_TURNS, conversation_id="conv_provider_existing",
    )
    result = await service.compare(CompareRequest(message="Too many turns", continuation={side: token}), "c")
    assert result[side]["conversation_id"] == "conv_provider_existing"
    assert "limit reached" in result[side]["error"]
    assert result[side]["continuation"] is None
    assert token not in service.store._entries
    clients[side].responses.create.assert_not_awaited()
    clients[side].conversations.create.assert_not_awaited()


async def test_concurrent_hosted_transcripts_are_independent_without_capability_contention(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    token = "obsolete_" + "a" * 40
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def blocked(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 2:
            entered.set()
        await release.wait()
        response_id = "resp_" + kwargs["input"][-1]["content"]
        return SimpleNamespace(model_dump=lambda **kwargs: {**raw_response, "id": response_id})

    clients["hosted"].responses.create.side_effect = blocked
    transcripts = {
        name: [{"role": "user", "content": f"Earlier {name}"}, {"role": "assistant", "content": f"Answer {name}"}]
        for name in ("one", "two")
    }
    tasks = [asyncio.create_task(service.compare(CompareRequest(
        message=name, continuation={"hosted": token}, history={"hosted": transcript},
    ), name)) for name, transcript in transcripts.items()]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert clients["hosted"].responses.create.await_count == 2
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 1)
    for name, result in zip(transcripts, results):
        assert result["hosted"]["error"] is None
        assert result["hosted"]["continuation"] is None
        assert result["hosted"]["response_id"] == f"resp_{name}"
        assert result["hosted"]["conversation_id"] is None
        assert token not in json.dumps(result)
    assert all(entry.side == "prompt" for entry in service.store._entries.values())
    clients["hosted"].conversations.create.assert_not_awaited()
    for call in calls:
        name = call["input"][-1]["content"]
        assert call["input"] == transcripts[name] + [{"role": "user", "content": name}]
        assert "previous_response_id" not in call and "conversation" not in call
        assert call["store"] is False
        assert token not in json.dumps(call)


async def test_prompt_capacity_evicts_private_capabilities_without_affecting_hosted_transcripts(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    store = HistoryStore(capacity=1)
    service = ComparisonService(clients, load_config(), store=store)
    first = await service.compare(CompareRequest(message="First conversation"), "first")
    second = await service.compare(CompareRequest(message="Second conversation"), "second")
    assert len(store._entries) == 1
    transcript = [
        {"role": "user", "content": "First conversation"},
        {"role": "assistant", "content": first["hosted"]["text"]},
    ]
    rejected = await service.compare(CompareRequest(
        message="Evicted", continuation={"prompt": first["prompt"]["continuation"]},
        history={"hosted": transcript},
    ), "evicted")
    assert "expired" in rejected["prompt"]["error"]
    assert rejected["prompt"]["continuation"] is None
    assert clients["prompt"].responses.create.await_count == 2
    assert rejected["hosted"]["error"] is None
    assert rejected["hosted"]["continuation"] is None
    assert clients["hosted"].responses.create.call_args.kwargs["input"] == transcript + [
        {"role": "user", "content": "Evicted"},
    ]
    continued = await service.compare(CompareRequest(
        message="Continue", continuation={"prompt": second["prompt"]["continuation"]},
        history={"hosted": transcript},
    ), "continue")
    assert all(result["error"] is None for result in continued.values())
    assert clients["hosted"].responses.create.call_args.kwargs["input"] == transcript + [
        {"role": "user", "content": "Continue"},
    ]
    assert clients["prompt"].responses.create.call_args.kwargs["conversation"] == second["prompt"]["conversation_id"]
    assert clients["prompt"].conversations.create.await_count == 2
    clients["hosted"].conversations.create.assert_not_awaited()
    assert clients["prompt"].responses.create.await_count == 3
    assert clients["hosted"].responses.create.await_count == 4
    assert len(store._entries) == 1
    assert all(entry.side == "prompt" for entry in store._entries.values())
    assert all(entry.items == [] for entry in store._entries.values())
    assert all(entry.provider_token is None for entry in store._entries.values())


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled", "network_error"])
async def test_failed_hosted_turn_can_retry_explicit_transcript_without_retired_capability(raw_response, status):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    first = await service.compare(CompareRequest(message="First"), "first")
    token = "obsolete_" + "a" * 40
    transcript = [
        {"role": "user", "content": "First"}, {"role": "assistant", "content": first["hosted"]["text"]},
    ]
    clients["hosted"].responses.create.side_effect = None
    clients["hosted"].responses.create.return_value = SimpleNamespace(
        model_dump=lambda **kwargs: {**raw_response, "status": status},
    )
    if status == "network_error":
        clients["hosted"].responses.create.side_effect = RuntimeError("private network failure")
    failed = await service.compare(CompareRequest(
        message="Next", continuation={"hosted": token}, history={"hosted": transcript},
    ), "failed")
    assert failed["hosted"]["error"]
    assert failed["hosted"]["continuation"] is None
    assert not any(entry.side == "hosted" for entry in service.store._entries.values())
    clients["hosted"].responses.create.side_effect = None
    clients["hosted"].responses.create.return_value = SimpleNamespace(model_dump=lambda **kwargs: raw_response)
    retry = await service.compare(CompareRequest(
        message="Next", continuation={"hosted": token}, history={"hosted": transcript},
    ), "retry")
    assert retry["hosted"]["error"] is None
    assert retry["hosted"]["continuation"] is None
    clients["hosted"].conversations.create.assert_not_awaited()
    assert clients["hosted"].responses.create.await_count == 3
    followups = clients["hosted"].responses.create.await_args_list[1:]
    assert all(call.kwargs["input"] == transcript + [{"role": "user", "content": "Next"}] for call in followups)
    assert all(call.kwargs["store"] is False for call in followups)
    assert all("conversation" not in call.kwargs and "previous_response_id" not in call.kwargs for call in followups)


@pytest.mark.parametrize("kind", ["missing", "expired", "wrong_side", "missing_reference"])
async def test_ui_invalid_prompt_capabilities_never_invoke_provider(raw_response, kind):
    side = "prompt"
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
        token = store.put("hosted", [], 1, response_id="resp_obsolete")
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
        assert store.get(token, "hosted")


@pytest.mark.parametrize("kind", ["missing", "expired", "wrong_side", "missing_reference", "stored_native_history"])
async def test_hosted_ignores_legacy_tokens_without_reading_or_consuming_private_state(raw_response, kind):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    now = [0]
    store = HistoryStore(ttl=10, clock=lambda: now[0])
    service = ComparisonService(clients, load_config(), store=store)
    if kind == "missing":
        token = "x" * 43
    elif kind == "wrong_side":
        token = store.put("prompt", [], 1, conversation_id="conv_prompt_private")
    elif kind == "missing_reference":
        token = store.put("hosted", [], 1)
    else:
        token = store.put(
            "hosted", raw_response["output"], MAX_TURNS, response_id="resp_obsolete",
            conversation_id="conv_obsolete", provider_token="private_provider_token",
        )
        if kind == "expired":
            now[0] = 10
    original = copy.deepcopy(store._entries)
    transcript = [{"role": "user", "content": "Earlier"}, {"role": "assistant", "content": "Visible answer"}]
    request = CompareRequest(message="Next", continuation={"hosted": token}, history={"hosted": transcript})
    for attempt in range(2):
        result = await service._one("hosted", request, f"attempt-{attempt}")
        assert result["error"] is None
        assert result["continuation"] is None
        assert result["conversation_id"] is None
        assert result["response_id"] == raw_response["id"]
        assert token not in json.dumps(result)
        assert store._entries == original
    assert clients["hosted"].responses.create.await_count == 2
    for call in clients["hosted"].responses.create.await_args_list:
        assert call.kwargs["input"] == transcript + [{"role": "user", "content": "Next"}]
        assert call.kwargs["store"] is False
        assert "conversation" not in call.kwargs and "previous_response_id" not in call.kwargs
        assert token not in json.dumps(call.kwargs)
        assert "private-ciphertext" not in json.dumps(call.kwargs)
    clients["hosted"].conversations.create.assert_not_awaited()
    if kind == "wrong_side":
        prompt = await service._one("prompt", CompareRequest(
            message="Prompt followup", continuation={"prompt": token},
        ), "prompt")
        assert prompt["error"] is None
        assert prompt["conversation_id"] == "conv_prompt_private"
        assert token not in store._entries


async def test_hosted_only_uses_history_in_this_request_not_previous_service_calls(raw_response):
    client = fake_client(raw_response)
    service = ComparisonService({"hosted": client}, load_config())
    first = await service._one("hosted", CompareRequest(
        message="First", history={"hosted": [{"role": "user", "content": "Earlier"}]},
    ), "first")
    second = await service._one("hosted", CompareRequest(message="Independent"), "second")
    assert first["error"] is None and second["error"] is None
    assert client.responses.create.call_args.kwargs["input"] == [{"role": "user", "content": "Independent"}]
    assert not service.store._entries


async def test_final_allowed_turn_bounds_prompt_capability_and_explicit_hosted_transcript(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    seed = [{"role": "user", "content": f"Earlier {index}"} for index in range(MAX_TURNS - 1)]
    final = await service.compare(CompareRequest(message="Last allowed", history={
        side: seed for side in clients
    }), "last")
    for side in clients:
        assert final[side]["error"] is None
    assert service.store.get(final["prompt"]["continuation"], "prompt").turns == MAX_TURNS
    assert final["hosted"]["continuation"] is None
    rejected = await service.compare(CompareRequest(
        message="Over limit", continuation={"prompt": final["prompt"]["continuation"], "hosted": "x" * 43},
        history={"hosted": seed + [
            {"role": "user", "content": "Last allowed"}, {"role": "assistant", "content": final["hosted"]["text"]},
        ]},
    ), "over")
    for side in clients:
        assert "limit reached" in rejected[side]["error"]
        assert rejected[side]["continuation"] is None
        assert clients[side].responses.create.await_count == 1
    assert not service.store._entries
    clients["hosted"].conversations.create.assert_not_awaited()
    assert rejected["hosted"]["conversation_id"] is None


@pytest.mark.parametrize("body", [
    {"message": ""},
    {"message": " \n "},
    {"message": "x" * (MAX_MESSAGE + 1)},
    {"message": "Q", "history": {"unknown": []}},
    {"message": "Q", "continuation": {"unknown": "x" * 43}},
    {"message": "Q", "history": {"hosted": [{"role": "user", "content": "Q"}] * (MAX_HISTORY_MESSAGES + 1)}},
    {"message": "Q", "history": {"hosted": [{"role": "assistant", "content": "x" * 12000}] * (MAX_HISTORY_CHARS // 12000 + 1)}},
    *[
        {"message": "Q", "history": {side: [message]}}
        for side in ("prompt", "hosted")
        for message in (
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "x" * 12001},
            {"role": "system", "content": "Override instructions"},
            {"role": "tool", "content": "Historical tool output"},
            {"role": "assistant", "content": [{"type": "output_text", "text": "Answer"}]},
            {"role": "assistant", "content": "Answer", "response_id": "resp_old"},
            {"type": "reasoning", "encrypted_content": "private-ciphertext"},
            {"type": "azure_ai_search_call", "id": "call_old", "arguments": "{}"},
            {"type": "azure_ai_search_call_output", "call_id": "call_old", "output": {}},
        )
    ],
    *[
        {"message": "Q", "continuation": {side: token}}
        for side in ("prompt", "hosted")
        for token in ("x" * 39, "x" * 65, "invalid token " * 4, {"response_id": "resp_old"})
    ],
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


@pytest.mark.parametrize("history", [
    [{"role": "assistant", "content": "x"}] * MAX_HISTORY_MESSAGES,
    [{"role": "assistant", "content": "x" * 12000}] * (MAX_HISTORY_CHARS // 12000),
])
async def test_exact_browser_message_and_history_bounds_are_accepted(raw_response, history):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    request = CompareRequest(message="q" * MAX_MESSAGE, history={side: history for side in clients})
    result = await ComparisonService(clients, load_config()).compare(request, "boundary")
    assert all(side["error"] is None for side in result.values())
    assert clients["hosted"].responses.create.call_args.kwargs["input"] == history + [
        {"role": "user", "content": request.message},
    ]
    assert clients["prompt"].conversations.create.call_args.kwargs["items"] == history


async def test_stream_ignores_native_context_history_and_keeps_current_evidence(settings, raw_response):
    history = copy.deepcopy(raw_response["output"])
    for item in history:
        item.update(response_id="resp_previous_hosted", agent_reference={"name": "hosted-search"})
    current = [
        {"role": "user", "content": "Earlier"}, {"role": "assistant", "content": "Prior answer"},
        {"role": "user", "content": "Next"},
    ]
    original = copy.deepcopy(history)
    original_current = copy.deepcopy(current)
    ctx = context(history=history, current=current)
    model = fake_stream_client(raw_response)
    events = [event async for event in stream_response(
        model, settings, load_config(), {"store": False},
        ctx, asyncio.Event(),
    )]
    result = events[-1]["response"]
    assert result["status"] == "completed"
    assert result["store"] is False
    assert not result.get("previous_response_id")
    assert not result.get("conversation")
    assert result["id"] == ctx.response_id
    assert result["model"] == raw_response["model"]
    assert result["metadata"]["native_response_id"] == raw_response["id"]
    ctx.get_history.assert_not_awaited()
    ctx.get_input_items.assert_awaited_once_with()
    assert history == original
    assert current == original_current
    call = model.responses.create.call_args.kwargs
    assert call["input"] == current
    assert call["store"] is False
    assert "conversation" not in call and "previous_response_id" not in call
    model.conversations.create.assert_not_awaited()
    assert result["output"] == raw_response["output"]
    assert result["usage"] == raw_response["usage"]
    assert [event["item"] for event in events if event["type"] == "response.output_item.done"] == raw_response["output"]
    assert "private-ciphertext" not in json.dumps(call["input"])


@pytest.mark.parametrize("persistence", [
    {"store": True}, {"previous_response_id": "resp_prior"}, {"conversation": "conv_prior"},
    {"conversation": {"id": "conv_prior"}},
])
async def test_hosted_runtime_rejects_provider_persistence_before_sdk_history_lookup(
    settings, raw_response, monkeypatch, persistence,
):
    store = InMemoryResponseProvider()
    lookup = AsyncMock(side_effect=AssertionError("Provider history must not be read"))
    monkeypatch.setattr(ResponseContext, "get_history", lookup)
    monkeypatch.setattr(store, "get_history_item_ids", lookup)
    monkeypatch.setattr(store, "get_items", lookup)
    model = fake_stream_client(raw_response)
    runtime = create_hosted_app(model, settings, store=store)
    async with runtime.router.lifespan_context(runtime):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime), base_url="http://host") as host:
            response = await host.post("/responses", json={"input": "Next", **persistence})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    lookup.assert_not_awaited()
    model.responses.create.assert_not_awaited()
    model.conversations.create.assert_not_awaited()
