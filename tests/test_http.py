import asyncio
import json
import re
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient
from azure.ai.agentserver.responses import InMemoryResponseProvider

from comparison.config import load_config, model_options
from comparison.contracts import MAX_RAW_BYTES, MAX_RAW_ITEMS, MAX_TURNS
from comparison.guard import RequestGuard
from comparison.service import ComparisonService
from comparison.state import HistoryStore
from conftest import fake_client, fake_stream_client
from hosted_agent.app import create_app as hosted_factory, inline_items
from web.app import create_app


def create_hosted_app(client, settings):
    return hosted_factory(client, settings, store=InMemoryResponseProvider())


def test_web_routes_contract_and_safe_validation(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    with TestClient(create_app(service)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health/readiness").status_code == 200
        config = client.get("/api/config").json()
        assert config["reasoning_effort"] == "low"
        assert config["reasoning_fixed"] is True
        assert config["telemetry"]["content_logging"] is False
        response = client.post("/api/compare", json={"message": "Question"})
        assert response.status_code == 200
        data = response.json()
        assert set(("comparison_id", "reasoning_effort", "prompt", "hosted")) <= data.keys()
        assert data["prompt"]["tool_evidence_available"]
        assert response.headers["cache-control"] == "no-store"
        assert "unsafe-inline" not in response.headers["content-security-policy"]
        validation = client.post("/api/compare", json={"message": "Question", "api_key": "secret"})
        assert validation.status_code == 422
        assert "secret" not in validation.text
        assert client.post("/api/compare", content="x").status_code == 415
        assert client.post(
            "/api/compare", json={"message": "Q"}, headers={"sec-fetch-site": "cross-site"},
        ).status_code == 403
        assert client.post(
            "/api/compare", content=b"x" * 65537, headers={"content-type": "application/json"},
        ).status_code == 413


async def test_hosted_protocol_preserves_native_items(settings, raw_response):
    downstream = fake_stream_client(raw_response)
    downstream.conversations.create.side_effect = None
    downstream.conversations.create.return_value = SimpleNamespace(id="conv_actual_project_model")
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            assert (await client.get("/readiness")).status_code == 200
            assert (await client.get("/health/readiness")).status_code == 200
            assert (await client.get("/health")).status_code == 200
            response = await client.post("/responses", json={
                "input": [{"role": "user", "content": "Question"}],
                "store": False, "stream": False, "reasoning": {"effort": "high"},
                "model": "ignored-client-model",
            }, headers={"x-client-comparison-id": "11111111-1111-1111-1111-111111111111"})
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["status"] == "completed", data
            assert [{k: v for k, v in item.items() if k != "response_id"} for item in data["output"]] == raw_response["output"]
            assert data["usage"] == raw_response["usage"]
            assert data["id"] != "resp_native"
            assert data["metadata"]["native_response_id"] == "resp_native"
            assert data["store"] is False
            assert not data.get("conversation")
            assert not data.get("previous_response_id")
            assert data["metadata"]["conversation_scope"] == "hosted_model"
            assert data["metadata"]["hosted_model_conversation_id"] == "conv_actual_project_model"
            assert re.fullmatch(r"[A-Za-z0-9_-]{43}", data["metadata"]["hosted_model_continuation"])
            args = downstream.responses.create.call_args.kwargs
            assert args["model"] == settings.model_deployment
            assert args["reasoning"] == {"effort": "low"}
            assert args["store"] is True
            assert args["stream"] is True
            assert args["tools"][0]["type"] == "azure_ai_search"
            assert "agent_reference" not in args
            assert "previous_response_id" not in args
            assert args["conversation"] == data["metadata"]["hosted_model_conversation_id"]
            downstream.conversations.create.assert_awaited_once_with(
                items=[], extra_headers=args["extra_headers"],
            )
            assert args["extra_headers"]["x-client-comparison-id"] == "11111111-1111-1111-1111-111111111111"
            assert args["input"][0]["content"] == [{"type": "input_text", "text": "Question"}]
            stored = await client.get(f"/responses/{data['id']}")
            assert stored.status_code == 404
            assert "private-ciphertext" not in stored.text
            assert data["metadata"]["hosted_model_continuation"] not in stored.text


async def test_hosted_failure_is_sanitized_without_continuation(settings, raw_response):
    downstream = fake_stream_client(raw_response)
    downstream.responses.create.side_effect = RuntimeError("secret=private; endpoint confidential")
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={"input": "Question", "store": False})
            assert response.status_code == 200
            assert response.json()["status"] == "failed"
            assert "private" not in response.text
            assert "confidential" not in response.text
            assert "hosted_model_continuation" not in response.json().get("metadata", {})
            assert response.json()["store"] is False


async def test_hosted_seeds_initial_history_once_without_replaying_native_model_history(settings, raw_response, monkeypatch):
    history = HistoryStore()
    monkeypatch.setattr("hosted_agent.app.HistoryStore", lambda: history)
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            first = await client.post("/responses", json={
                "input": [
                    {"role": "user", "content": "Earlier"},
                    {"role": "assistant", "content": "Earlier answer"},
                    {"role": "user", "content": "Question"},
                ],
                "store": False,
            })
            assert first.status_code == 200
            assert first.json()["status"] == "completed", first.text
            metadata = first.json()["metadata"]
            conversation_id = metadata["hosted_model_conversation_id"]
            token = metadata["hosted_model_continuation"]
            seed = downstream.conversations.create.call_args.kwargs["items"]
            assert len(seed) == 2
            assert seed[0]["content"] == [{"type": "input_text", "text": "Earlier"}]
            assert seed[1]["role"] == "assistant"
            assert seed[1]["content"] == [{"type": "input_text", "text": "Earlier answer"}]
            assert all("response_id" not in item for item in seed)
            first_args = downstream.responses.create.call_args.kwargs
            assert first_args["conversation"] == conversation_id
            assert len(first_args["input"]) == 1
            assert first_args["input"][0]["content"] == [{"type": "input_text", "text": "Question"}]
            snapshot = history.get(token, "hosted_model:_local")
            assert snapshot.items == []
            assert snapshot.turns == 2
            assert snapshot.conversation_id == conversation_id
            assert snapshot.response_id is None
            response = await client.post("/responses", json={
                "input": "Follow up", "store": False,
            }, headers={"x-client-hosted-continuation": token})
            assert response.status_code == 200
            assert response.json()["status"] == "completed", response.text
            args = downstream.responses.create.call_args.kwargs
            assert args["conversation"] == conversation_id
            assert args["store"] is True
            assert args["stream"] is True
            assert len(args["input"]) == 1
            assert args["input"][0]["content"] == [{"type": "input_text", "text": "Follow up"}]
            for item in raw_response["output"]:
                assert item["id"] not in json.dumps(args["input"])
            assert "private-ciphertext" not in json.dumps(args["input"])
            assert "previous_response_id" not in args
            assert "x-client-hosted-continuation" not in args["extra_headers"]
            downstream.conversations.create.assert_awaited_once()
            assert downstream.responses.create.await_count == 2
            metadata = response.json()["metadata"]
            assert metadata["hosted_model_conversation_id"] == conversation_id
            assert metadata["conversation_scope"] == "hosted_model"
            assert metadata["hosted_model_continuation"] != token
            assert token not in history._entries
            snapshot = history.get(metadata["hosted_model_continuation"], "hosted_model:_local")
            assert snapshot.items == []
            assert snapshot.turns == 3
            assert snapshot.conversation_id == conversation_id


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("options", [
    {"store": True},
    {"conversation": "conv_private"},
    {"conversation": {"id": "conv_private"}},
    {"previous_response_id": "resp_private"},
    {"background": True},
])
async def test_hosted_guard_rejects_outer_persistence_before_sdk(settings, raw_response, options, stream):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={
                "input": "private request content", "store": False, "stream": stream, **options,
            })
            assert response.status_code == 400
            assert response.headers["content-type"].startswith("application/json")
            assert response.json()["error"]["code"] == "invalid_request"
            assert "private" not in response.text
            downstream.conversations.create.assert_not_awaited()
            downstream.responses.create.assert_not_awaited()


@pytest.mark.parametrize("body", ["{", "null", "[]", '"private"', "42", "true"])
async def test_hosted_guard_rejects_malformed_or_nonobject_json(settings, raw_response, body):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post(
                "/responses", content=body, headers={"content-type": "application/json"},
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_request"
            assert "private" not in response.text
            downstream.conversations.create.assert_not_awaited()
            downstream.responses.create.assert_not_awaited()


@pytest.mark.parametrize("field", [
    "model", "tools", "instructions", "agent_reference", "reasoning", "tool_choice",
    "temperature", "top_p", "max_output_tokens", "parallel_tool_calls", "include",
])
async def test_hosted_guard_strips_overrides_before_sdk_validation(settings, raw_response, field):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={
                "input": "Question", "store": False, field: {"invalid": "private override"},
            })
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "completed", response.text
            args = downstream.responses.create.call_args.kwargs
            assert args == {
                **model_options(settings, load_config()),
                "conversation": data["metadata"]["hosted_model_conversation_id"],
                "input": args["input"], "stream": True, "extra_headers": args["extra_headers"],
            }
            assert "private override" not in json.dumps(args)
            assert "private override" not in response.text
            assert len(args["input"]) == 1
            assert args["input"][0]["content"] == [{"type": "input_text", "text": "Question"}]


@pytest.mark.parametrize("options", [
    {},
    {"store": False, "background": False, "conversation": None, "previous_response_id": None},
])
async def test_hosted_guard_defaults_outer_response_to_nonpersistent(settings, raw_response, options):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={"input": "Question", **options})
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "completed"
            assert data["store"] is False
            assert (await client.get(f"/responses/{data['id']}")).status_code == 404
            assert downstream.responses.create.call_args.kwargs["store"] is True


@pytest.mark.parametrize(("headers", "body", "status"), [
    ({"content-type": "text/plain"}, b"private", 415),
    ({"content-type": "application/json", "sec-fetch-site": "cross-site"}, b'{"input":"private"}', 403),
    ({"content-type": "application/json"}, b"x" * (MAX_RAW_BYTES + 1), 413),
])
async def test_hosted_http_security_guard_blocks_before_model(settings, raw_response, headers, body, status):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", content=body, headers=headers)
            assert response.status_code == status
            assert "private" not in response.text
            downstream.conversations.create.assert_not_awaited()
            downstream.responses.create.assert_not_awaited()


async def test_hosted_continuation_is_user_partitioned_and_single_use(settings, raw_response, monkeypatch):
    history = HistoryStore()
    monkeypatch.setattr("hosted_agent.app.HistoryStore", lambda: history)
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            first = await client.post("/responses", json={"input": "Question", "store": False}, headers={
                "x-agent-user-id": "alice", "x-agent-foundry-call-id": "opaque-call-one",
            })
            assert first.status_code == 200
            assert first.json()["status"] == "completed", first.text
            metadata = first.json()["metadata"]
            token = metadata["hosted_model_continuation"]
            conversation_id = metadata["hosted_model_conversation_id"]
            assert token not in (conversation_id, first.json()["id"], raw_response["id"])
            assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
            assert history.get(token, "hosted_model:alice").conversation_id == conversation_id
            for mock in (downstream.conversations.create, downstream.responses.create):
                headers = mock.call_args.kwargs["extra_headers"]
                assert headers["x-agent-foundry-call-id"] == "opaque-call-one"
                assert "x-agent-user-id" not in headers
            for user in ("bob", None):
                headers = {"x-client-hosted-continuation": token}
                if user:
                    headers["x-agent-user-id"] = user
                rejected = await client.post(
                    "/responses", json={"input": "Follow up", "store": False}, headers=headers,
                )
                assert rejected.status_code == 200
                assert rejected.json()["status"] == "failed"
                assert rejected.json()["error"]["code"] == "conversation_expired"
                assert token not in rejected.text
                assert conversation_id not in rejected.text
                assert "hosted_model_continuation" not in rejected.json().get("metadata", {})
                assert token in history._entries
            assert downstream.responses.create.await_count == 1
            continued = await client.post("/responses", json={"input": "Follow up", "store": False}, headers={
                "x-agent-user-id": "alice", "x-client-hosted-continuation": token,
                "x-agent-foundry-call-id": "opaque-call-two",
            })
            assert continued.json()["status"] == "completed", continued.text
            rotated = continued.json()["metadata"]["hosted_model_continuation"]
            assert rotated != token
            assert history.get(rotated, "hosted_model:alice").conversation_id == conversation_id
            args = downstream.responses.create.call_args.kwargs
            assert args["conversation"] == conversation_id
            assert args["extra_headers"]["x-agent-foundry-call-id"] == "opaque-call-two"
            assert "x-client-hosted-continuation" not in args["extra_headers"]
            replay = await client.post("/responses", json={"input": "Replay", "store": False}, headers={
                "x-agent-user-id": "alice", "x-client-hosted-continuation": token,
            })
            assert replay.json()["status"] == "failed"
            assert replay.json()["error"]["code"] == "conversation_expired"
            assert "hosted_model_continuation" not in replay.json().get("metadata", {})
            downstream.conversations.create.assert_awaited_once()
            assert downstream.responses.create.await_count == 2


@pytest.mark.parametrize("invalid_token", ["unknown", "expired", "conversation_id", "response_id"])
async def test_hosted_cannot_continue_by_provider_id_or_expired_token(settings, raw_response, monkeypatch, invalid_token):
    now = 0
    history = HistoryStore(ttl=1, clock=lambda: now)
    monkeypatch.setattr("hosted_agent.app.HistoryStore", lambda: history)
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            first = await client.post("/responses", json={"input": "Question", "store": False})
            assert first.json()["status"] == "completed"
            data = first.json()
            token = {
                "unknown": "x" * 43,
                "expired": data["metadata"]["hosted_model_continuation"],
                "conversation_id": data["metadata"]["hosted_model_conversation_id"],
                "response_id": data["id"],
            }[invalid_token]
            if invalid_token == "expired":
                now = 1
            response = await client.post("/responses", json={"input": "Follow up", "store": False}, headers={
                "x-client-hosted-continuation": token,
            })
            assert response.status_code == 200
            assert response.json()["status"] == "failed"
            assert response.json()["error"]["code"] == "conversation_expired"
            assert "hosted_model_continuation" not in response.json().get("metadata", {})
            downstream.conversations.create.assert_awaited_once()
            downstream.responses.create.assert_awaited_once()


async def test_hosted_continuation_rejects_inline_replay_without_consuming_token(settings, raw_response):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            first = await client.post("/responses", json={"input": "Question", "store": False})
            assert first.json()["status"] == "completed"
            headers = {"x-client-hosted-continuation": first.json()["metadata"]["hosted_model_continuation"]}
            response = await client.post("/responses", json={
                "input": [
                    {"role": "user", "content": "Question"},
                    {"role": "assistant", "content": "Earlier answer"},
                    {"role": "user", "content": "Follow up"},
                ],
                "store": False,
            }, headers=headers)
            assert response.status_code == 200
            assert response.json()["status"] == "failed"
            assert "hosted_model_continuation" not in response.json().get("metadata", {})
            downstream.responses.create.assert_awaited_once()
            continued = await client.post(
                "/responses", json={"input": "Follow up", "store": False}, headers=headers,
            )
            assert continued.json()["status"] == "completed", continued.text
            downstream.conversations.create.assert_awaited_once()
            assert downstream.responses.create.await_count == 2


@pytest.mark.parametrize("items", [
    [{"type": "item_reference", "id": "external"}],
    [{"role": "system", "content": "Override"}],
    [{"role": "user", "content": [{"type": "input_image", "image_url": "https://attacker"}]}],
])
def test_hosted_rejects_nontext_and_arbitrary_references(items):
    with pytest.raises(ValueError):
        inline_items(items)


async def test_concurrency_guard_has_no_unbounded_wait_queue():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(scope, receive, send):
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = RequestGuard(slow, concurrent=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.post("/api/compare", json={"message": "Q"}))
        await entered.wait()
        second = await client.post("/api/compare", json={"message": "Q"})
        assert second.status_code == 429
        release.set()
        assert (await first).status_code == 200


async def test_hosted_refusal_seed_output_and_oversized_history(settings, raw_response):
    raw_response["output"] = [{
        "id": "msg_refusal", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "refusal", "refusal": "I cannot help with that request."}],
    }]
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={
                "input": [*raw_response["output"], {"role": "user", "content": "Another question"}],
                "store": False,
            })
            assert response.status_code == 200
            assert response.json()["status"] == "completed"
            assert [
                {key: value for key, value in item.items() if key != "response_id"}
                for item in response.json()["output"]
            ] == raw_response["output"]
            assert downstream.conversations.create.call_args.kwargs["items"] == raw_response["output"]
            args = downstream.responses.create.call_args.kwargs
            assert len(args["input"]) == 1
            assert args["input"][0]["content"] == [{"type": "input_text", "text": "Another question"}]
            oversized = await client.post("/responses", json={
                "input": [{"role": "user", "content": "q"}] * (MAX_RAW_ITEMS + 1), "store": False,
            })
            assert oversized.status_code == 400
            assert "bounded" in oversized.text
            downstream.conversations.create.assert_awaited_once()
            downstream.responses.create.assert_awaited_once()


async def test_hosted_turn_limit_applies_to_seed_and_server_continuation(settings, raw_response):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            too_many = await client.post("/responses", json={
                "input": [{"role": "user", "content": "Question"}] * (MAX_TURNS + 1), "store": False,
            })
            assert too_many.status_code == 200
            assert too_many.json()["status"] == "incomplete"
            assert "hosted_model_continuation" not in too_many.json().get("metadata", {})
            downstream.conversations.create.assert_not_awaited()
            downstream.responses.create.assert_not_awaited()
            at_limit = await client.post("/responses", json={
                "input": [{"role": "user", "content": "Question"}] * MAX_TURNS, "store": False,
            })
            assert at_limit.json()["status"] == "completed", at_limit.text
            assert len(downstream.conversations.create.call_args.kwargs["items"]) == MAX_TURNS - 1
            assert len(downstream.responses.create.call_args.kwargs["input"]) == 1
            token = at_limit.json()["metadata"]["hosted_model_continuation"]
            overflow = await client.post("/responses", json={"input": "One too many", "store": False}, headers={
                "x-client-hosted-continuation": token,
            })
            assert overflow.status_code == 200
            assert overflow.json()["status"] == "incomplete"
            assert overflow.json()["error"]["code"] == "upstream_interrupted"
            assert "hosted_model_continuation" not in overflow.json().get("metadata", {})
            downstream.conversations.create.assert_awaited_once()
            downstream.responses.create.assert_awaited_once()


async def test_sse_preserves_raw_native_item(settings, raw_response):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={"input": "Question", "stream": True, "store": False})
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert "event: response.output_item.done" in response.text
            assert "event: response.completed" in response.text
            assert '"id": "search_1"' in response.text
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines() if line.startswith("data: {")
            ]
            terminal = next(event["response"] for event in events if event["type"] == "response.completed")
            assert terminal["store"] is False
            assert terminal["metadata"]["conversation_scope"] == "hosted_model"
            assert re.fullmatch(r"[A-Za-z0-9_-]{43}", terminal["metadata"]["hosted_model_continuation"])
            assert terminal["metadata"]["hosted_model_conversation_id"] == (
                downstream.responses.create.call_args.kwargs["conversation"]
            )
            assert terminal["metadata"]["native_response_id"] == raw_response["id"]
            assert terminal["usage"] == raw_response["usage"]
            assert [
                {key: value for key, value in item.items() if key != "response_id"}
                for item in terminal["output"]
            ] == raw_response["output"]
            assert (await client.get(f"/responses/{terminal['id']}")).status_code == 404


async def test_web_disconnect_cancels_both_fanout_calls(raw_response):
    from comparison.cancellation import while_connected
    from comparison.contracts import CompareRequest

    started, cancelled = set(), set()
    disconnected = asyncio.Event()
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    for side in clients:
        async def slow(side=side, **kwargs):
            started.add(side)
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.add(side)
        clients[side].responses.create.side_effect = slow
    service = ComparisonService(clients, load_config())

    async def is_disconnected():
        return disconnected.is_set()

    work = asyncio.create_task(while_connected(
        service.compare(CompareRequest(message="Question"), "c"), is_disconnected,
    ))
    for _ in range(20):
        if len(started) == 2:
            break
        await asyncio.sleep(0.01)
    disconnected.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(work, 1)
    assert cancelled == {"prompt", "hosted"}
    assert len(service.store._entries) == 0
