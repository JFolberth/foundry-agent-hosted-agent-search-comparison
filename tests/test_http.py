import asyncio
import json
from html.parser import HTMLParser
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.testclient import TestClient
from azure.ai.agentserver.responses import InMemoryResponseProvider, ResponseContext

from comparison.config import load_config, model_options
from comparison.contracts import MAX_HISTORY_CHARS, MAX_HISTORY_MESSAGES, MAX_RAW_BYTES, MAX_RAW_ITEMS, MAX_TURNS
from comparison.guard import RequestGuard
from comparison.service import ComparisonService
from conftest import fake_client, fake_stream_client
from hosted_agent.app import create_app as hosted_factory, inline_items
from web.app import create_app


def create_hosted_app(client, settings):
    return hosted_factory(client, settings, store=InMemoryResponseProvider())


@pytest.fixture(autouse=True)
def no_hosted_history(monkeypatch):
    history = AsyncMock(side_effect=AssertionError("Stateless requests must not fetch history"))
    history_ids = AsyncMock(side_effect=AssertionError("Stateless requests must not resolve stored history"))
    monkeypatch.setattr(ResponseContext, "get_history", history)
    monkeypatch.setattr(InMemoryResponseProvider, "get_history_item_ids", history_ids)
    yield
    history.assert_not_awaited()
    history_ids.assert_not_awaited()


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


def test_web_page_referenced_assets_are_served(raw_response):
    class PageAssets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.assets = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "link" and attrs.get("rel") == "stylesheet":
                self.assets.append((attrs["href"], "text/css"))
            elif tag == "script" and "src" in attrs:
                self.assets.append((attrs["src"], "javascript"))

    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    with TestClient(create_app(ComparisonService(clients, load_config()))) as client:
        page = client.get("/")
        assert page.status_code == 200
        assets = PageAssets()
        assets.feed(page.text)
        assert len(assets.assets) >= 2
        for url, content_type in assets.assets:
            assert url.startswith("/static/")
            response = client.get(url)
            assert response.status_code == 200, url
            assert content_type in response.headers["content-type"]
            assert response.content
            assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get("/static/missing.css").status_code == 404
        assert client.get("/api/config").status_code == 200


async def test_hosted_protocol_preserves_native_items(settings, raw_response):
    downstream = fake_stream_client(raw_response)
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
            assert not {
                "conversation_scope", "hosted_model_conversation_id", "hosted_model_continuation",
            } & data["metadata"].keys()
            assert all(item.get("response_id", data["id"]) == data["id"] for item in data["output"])
            args = downstream.responses.create.call_args.kwargs
            assert args["model"] == settings.model_deployment
            assert args["reasoning"] == {"effort": "low"}
            assert args["max_output_tokens"] == 4096
            assert args["include"] == ["reasoning.encrypted_content"]
            assert args["store"] is False
            assert args["stream"] is True
            assert args["tools"][0]["type"] == "azure_ai_search"
            assert "agent_reference" not in args
            assert "previous_response_id" not in args
            assert "conversation" not in args
            downstream.conversations.create.assert_not_awaited()
            assert args["extra_headers"]["x-client-comparison-id"] == "11111111-1111-1111-1111-111111111111"
            assert args["input"][0]["content"] == [{"type": "input_text", "text": "Question"}]
            stored = await client.get(f"/responses/{data['id']}")
            assert stored.status_code == 404
            assert "private-ciphertext" not in stored.text


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


async def test_hosted_followup_resends_only_explicit_transcript(settings, raw_response):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    transcript = [
        {"role": "user", "content": "Earlier"},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": "Question"},
    ]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            first = await client.post("/responses", json={"input": transcript})
            assert first.status_code == 200
            assert first.json()["status"] == "completed", first.text
            first_args = downstream.responses.create.call_args.kwargs
            assert len(first_args["input"]) == 3
            assert [item["role"] for item in first_args["input"]] == ["user", "assistant", "user"]
            assert [item["content"] for item in first_args["input"]] == [
                [{"type": kind, "text": text}]
                for kind, text in (
                    ("input_text", "Earlier"), ("output_text", "Earlier answer"), ("input_text", "Question")
                )
            ]
            assert first_args["store"] is False
            response_id = first.json()["id"]
            assert response_id != raw_response["id"]
            assert first.json()["store"] is False
            stored = await client.get(f"/responses/{response_id}")
            assert stored.status_code == 404
            transcript.extend([
                {"role": "assistant", "content": "Indexed answer."},
                {"role": "user", "content": "Follow up"},
            ])
            response = await client.post("/responses", json={"input": transcript, "store": False})
            assert response.status_code == 200
            assert response.json()["status"] == "completed", response.text
            args = downstream.responses.create.call_args.kwargs
            assert "conversation" not in args
            assert args["store"] is False
            assert args["stream"] is True
            assert args["input"] == [
                {"type": "message", "role": item["role"], "content": [{
                    "type": "output_text" if item["role"] == "assistant" else "input_text", "text": item["content"],
                }]}
                for item in transcript
            ]
            assert "private-ciphertext" not in json.dumps(args["input"])
            assert "search_1" not in json.dumps(args["input"])
            assert all("response_id" not in item and "agent_reference" not in item for item in args["input"])
            assert "previous_response_id" not in args
            assert "x-client-hosted-continuation" not in args["extra_headers"]
            downstream.conversations.create.assert_not_awaited()
            assert downstream.responses.create.await_count == 2
            assert not response.json().get("previous_response_id")
            assert not response.json().get("conversation")
            assert response.json()["id"] not in (response_id, raw_response["id"])
            assert response.json()["metadata"]["native_response_id"] == raw_response["id"]
            assert "hosted_model_continuation" not in response.json()["metadata"]
            assert (await client.get(f"/responses/{response.json()['id']}")).status_code == 404


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("options", [
    {"background": True},
    {"store": True},
    {"conversation": "conv_private"},
    {"conversation": {"id": "conv_private"}},
    {"previous_response_id": "resp_private"},
])
async def test_hosted_guard_rejects_persistence_and_background_before_sdk(settings, raw_response, stream, options):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={
                "input": "private request content", "stream": stream, **options,
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
                "input": args["input"], "stream": True, "extra_headers": args["extra_headers"],
            }
            assert args["store"] is False
            downstream.conversations.create.assert_not_awaited()
            assert "private override" not in json.dumps(args)
            assert "private override" not in response.text
            assert len(args["input"]) == 1
            assert args["input"][0]["content"] == [{"type": "input_text", "text": "Question"}]


@pytest.mark.parametrize("options", [
    {},
    {"store": False},
    {"store": False, "background": False, "conversation": None, "previous_response_id": None},
])
@pytest.mark.parametrize("explicit_provider", [False, True])
async def test_hosted_defaults_to_unstored_with_in_memory_provider(
    settings, raw_response, options, explicit_provider, monkeypatch,
):
    downstream = fake_stream_client(raw_response)
    provider = InMemoryResponseProvider()
    factory = Mock(return_value=provider)
    monkeypatch.setattr("hosted_agent.app.InMemoryResponseProvider", factory)
    app = hosted_factory(downstream, settings, **({"store": provider} if explicit_provider else {}))
    if explicit_provider:
        factory.assert_not_called()
    else:
        factory.assert_called_once_with()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={"input": "Question", **options})
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "completed"
            assert data["store"] is False
            stored = await client.get(f"/responses/{data['id']}")
            assert stored.status_code == 404
            assert "private-ciphertext" not in stored.text
            args = downstream.responses.create.call_args.kwargs
            assert args["store"] is False
            assert "conversation" not in args
            assert "previous_response_id" not in args
            downstream.conversations.create.assert_not_awaited()


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


async def test_hosted_transcript_can_branch_without_server_history(settings, raw_response):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            first = await client.post("/responses", json={"input": "Question"}, headers={
                "x-agent-user-id": "alice", "x-agent-foundry-call-id": "opaque-call-one",
            })
            assert first.status_code == 200
            assert first.json()["status"] == "completed", first.text
            response_id = first.json()["id"]
            initial_input = downstream.responses.create.call_args.kwargs["input"]
            headers = downstream.responses.create.call_args.kwargs["extra_headers"]
            assert headers["x-agent-foundry-call-id"] == "opaque-call-one"
            assert "x-agent-user-id" not in headers
            branch_ids = set()
            for message in ("Follow up", "Different follow up"):
                continued = await client.post("/responses", json={
                    "input": [
                        {"role": "user", "content": "Question"},
                        {"role": "assistant", "content": "Indexed answer."},
                        {"role": "user", "content": message},
                    ], "store": False,
                }, headers={
                    "x-agent-user-id": "alice", "x-agent-foundry-call-id": "opaque-call-two",
                    "x-client-hosted-continuation": "obsolete-private-token",
                })
                assert continued.status_code == 200
                assert continued.json()["status"] == "completed", continued.text
                assert not continued.json().get("previous_response_id")
                assert not continued.json().get("conversation")
                assert continued.json()["store"] is False
                branch_ids.add(continued.json()["id"])
                stored = await client.get(f"/responses/{continued.json()['id']}")
                assert stored.status_code == 404
                args = downstream.responses.create.call_args.kwargs
                assert len(args["input"]) == 3
                assert args["input"][0] == initial_input[0]
                assert args["input"][1] == {
                    "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Indexed answer."}],
                }
                assert args["input"][-1]["content"] == [{"type": "input_text", "text": message}]
                assert args["store"] is False
                assert "conversation" not in args
                assert "previous_response_id" not in args
                assert args["extra_headers"]["x-agent-foundry-call-id"] == "opaque-call-two"
                assert "x-agent-user-id" not in args["extra_headers"]
                assert "x-client-hosted-continuation" not in args["extra_headers"]
                assert "obsolete-private-token" not in continued.text
                assert "hosted_model_continuation" not in continued.json()["metadata"]
            assert len(branch_ids) == 2
            assert response_id not in branch_ids
            assert (await client.get(f"/responses/{response_id}")).status_code == 404
            downstream.conversations.create.assert_not_awaited()
            assert downstream.responses.create.await_count == 3
            fresh = await client.post("/responses", json={"input": "Fresh question"})
            assert fresh.json()["status"] == "completed"
            assert downstream.responses.create.call_args.kwargs["input"] == [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fresh question"}]},
            ]


@pytest.mark.parametrize("stream", [False, True])
async def test_hosted_rejects_previous_response_even_for_actual_response_id(settings, raw_response, stream):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            first = await client.post("/responses", json={"input": "Question", "store": False})
            assert first.json()["status"] == "completed"
            response_id = first.json()["id"]
            assert (await client.get(f"/responses/{response_id}")).status_code == 404
            response = await client.post("/responses", json={
                "input": "private follow up", "previous_response_id": response_id, "stream": stream,
            })
            assert response.status_code == 400
            assert response.headers["content-type"].startswith("application/json")
            assert response.json()["error"]["code"] == "invalid_request"
            assert "private follow up" not in response.text
            assert "private-ciphertext" not in response.text
            downstream.conversations.create.assert_not_awaited()
            downstream.responses.create.assert_awaited_once()


@pytest.mark.parametrize("conversation", [None, "conv_provider", {"id": "conv_provider"}])
def test_web_hosted_resends_browser_transcript_and_reports_only_provider_ids(raw_response, conversation):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    hosted_raw = {**raw_response, "id": "resp_hosted_actual", "conversation": conversation}
    clients["hosted"] = fake_client(hosted_raw)
    clients["hosted"].conversations.create.side_effect = AssertionError("Hosted conversations are not created")
    with TestClient(create_app(ComparisonService(clients, load_config()))) as client:
        first = client.post("/api/compare", json={"message": "Question"})
        assert first.status_code == 200
        data = first.json()
        transcript = [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": data["hosted"]["text"]},
        ]
        second = client.post("/api/compare", json={
            "message": "Follow up", "history": {"hosted": transcript},
            "continuation": {"prompt": data["prompt"]["continuation"], "hosted": None},
        })
        assert second.status_code == 200
        for result in (data["hosted"], second.json()["hosted"]):
            assert result["error"] is None
            assert result["continuation"] is None
            assert result["response_id"] == hosted_raw["id"]
            assert result["conversation_id"] == ("conv_provider" if conversation else None)
            if conversation:
                assert "Provider-reported" in result["conversation_note"]
            else:
                assert "no conversation ID was reported" in result["conversation_note"]
                assert "not stored" in result["conversation_note"]
        calls = clients["hosted"].responses.create.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["input"] == [{"role": "user", "content": "Question"}]
        assert calls[1].kwargs["input"] == [*transcript, {"role": "user", "content": "Follow up"}]
        for call in calls:
            assert call.kwargs["store"] is False
            assert call.kwargs["stream"] is False
            assert call.kwargs["max_output_tokens"] == 4096
            assert call.kwargs["include"] == ["reasoning.encrypted_content"]
            assert "reasoning" not in call.kwargs
            assert "previous_response_id" not in call.kwargs
            assert "conversation" not in call.kwargs
        clients["hosted"].conversations.create.assert_not_awaited()


@pytest.mark.parametrize("bound", ["messages", "characters", "turns"])
def test_web_rejects_oversized_hosted_transcript_before_hosted_call(raw_response, bound):
    if bound == "messages":
        history = [{"role": "assistant", "content": "Earlier"}] * (MAX_HISTORY_MESSAGES + 1)
    elif bound == "characters":
        history = [{"role": "assistant", "content": "x" * (MAX_HISTORY_CHARS // 3 + 1)}] * 3
    else:
        history = [{"role": "user", "content": "Earlier"}] * MAX_TURNS
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    with TestClient(create_app(ComparisonService(clients, load_config()))) as client:
        response = client.post("/api/compare", json={"message": "Question", "history": {"hosted": history}})
    if bound == "turns":
        assert response.status_code == 200
        assert response.json()["hosted"]["error"]
        assert response.json()["hosted"]["continuation"] is None
    else:
        assert response.status_code == 422
    clients["hosted"].responses.create.assert_not_awaited()
    clients["hosted"].conversations.create.assert_not_awaited()


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
            args = downstream.responses.create.call_args.kwargs
            assert len(args["input"]) == 2
            assert args["input"][:-1] == raw_response["output"]
            assert args["input"][-1]["content"] == [{"type": "input_text", "text": "Another question"}]
            oversized = await client.post("/responses", json={
                "input": [{"role": "user", "content": "q"}] * (MAX_RAW_ITEMS + 1), "store": False,
            })
            assert oversized.status_code == 400
            assert "bounded" in oversized.text
            downstream.conversations.create.assert_not_awaited()
            downstream.responses.create.assert_awaited_once()


async def test_hosted_turn_limit_applies_to_each_inline_transcript(settings, raw_response):
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
                "input": [{"role": "user", "content": "Question"}] * MAX_TURNS,
            })
            assert at_limit.json()["status"] == "completed", at_limit.text
            assert len(downstream.responses.create.call_args.kwargs["input"]) == MAX_TURNS
            overflow = await client.post("/responses", json={
                "input": [
                    *[{"role": "user", "content": "Question"}] * MAX_TURNS,
                    {"role": "assistant", "content": "Indexed answer."},
                    {"role": "user", "content": "One too many"},
                ],
            })
            assert overflow.status_code == 200
            assert overflow.json()["status"] == "incomplete"
            assert overflow.json()["error"]["code"] == "upstream_interrupted"
            assert "hosted_model_continuation" not in overflow.json().get("metadata", {})
            downstream.conversations.create.assert_not_awaited()
            downstream.responses.create.assert_awaited_once()


@pytest.mark.parametrize("bound", ["items", "bytes"])
async def test_hosted_bounds_apply_to_resent_transcript(settings, raw_response, bound):
    if bound == "items":
        items = [
            {"role": "assistant", "content": f"Earlier answer {index}"}
            for index in range(MAX_RAW_ITEMS - 1)
        ]
        items.append({"role": "user", "content": "Question"})
    else:
        items = [{"role": "user", "content": "q" * (MAX_RAW_BYTES // 2)}]
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            first = await client.post("/responses", json={"input": items})
            assert first.status_code == 200
            assert first.json()["status"] == "completed", first.text
            overflow = await client.post("/responses", json={
                "input": [
                    *items,
                    {"role": "assistant", "content": "x" * (MAX_RAW_BYTES // 2) if bound == "bytes" else "Answer"},
                    {"role": "user", "content": "Follow up"},
                ],
            })
            assert overflow.status_code == (400 if bound == "items" else 413)
            assert (await client.get(f"/responses/{first.json()['id']}")).status_code == 404
            downstream.conversations.create.assert_not_awaited()
            downstream.responses.create.assert_awaited_once()


@pytest.mark.parametrize("options", [{}, {"store": False}])
@pytest.mark.parametrize("history", [
    [],
    [{"role": "user", "content": "Earlier question"}, {"role": "assistant", "content": "Earlier answer"}],
])
async def test_sse_preserves_current_turn_native_items_without_storage(settings, raw_response, options, history):
    downstream = fake_stream_client(raw_response)
    app = create_hosted_app(downstream, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            response = await client.post("/responses", json={
                "input": [*history, {"role": "user", "content": "Question"}], "stream": True, **options,
            })
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
            assert terminal["id"] != raw_response["id"]
            assert not {
                "conversation_scope", "hosted_model_conversation_id", "hosted_model_continuation",
            } & terminal["metadata"].keys()
            lifecycle = [
                event["response"] for event in events
                if event["type"] in ("response.created", "response.in_progress", "response.completed")
            ]
            assert len(lifecycle) == 3
            assert all(envelope["id"] == terminal["id"] and envelope["store"] is False for envelope in lifecycle)
            done_items = [event["item"] for event in events if event["type"] == "response.output_item.done"]
            assert [
                {key: value for key, value in item.items() if key != "response_id"}
                for item in done_items
            ] == raw_response["output"]
            assert all(item["response_id"] == terminal["id"] for item in done_items)
            assert [event["sequence_number"] for event in events] == list(range(len(events)))
            assert terminal["metadata"]["native_response_id"] == raw_response["id"]
            assert terminal["usage"] == raw_response["usage"]
            assert [
                {key: value for key, value in item.items() if key != "response_id"}
                for item in terminal["output"]
            ] == raw_response["output"]
            stored = await client.get(f"/responses/{terminal['id']}")
            assert stored.status_code == 404
            assert not terminal.get("previous_response_id")
            assert not terminal.get("conversation")
            args = downstream.responses.create.call_args.kwargs
            assert args["store"] is False
            assert "conversation" not in args
            assert "previous_response_id" not in args
            assert [(item["role"], item["content"][0]["text"]) for item in args["input"]] == [
                *((item["role"], item["content"]) for item in history), ("user", "Question"),
            ]
            downstream.conversations.create.assert_not_awaited()


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
