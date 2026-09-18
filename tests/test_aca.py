import asyncio
import json
from unittest.mock import Mock

import pytest

from comparison.aca import ACAClient
from comparison.clients import open_clients
from comparison.config import Settings, load_config, model_options
from comparison.agents import AGENT_SIDES
from comparison.contracts import CompareRequest, MAX_RAW_BYTES
from comparison.service import ComparisonService
from conftest import fake_client, fake_stream_client
from hosted_agent.app import create_app
from contextlib import AsyncExitStack
from test_clients import Credential
import httpx


class Content:
    async def iter_chunked(self, size):
        yield b'{"id":"resp_aca","object":"response","created_at":1,"status":"completed","model":"aca-model","output":[],"parallel_tool_calls":true,"tool_choice":"auto","tools":[]}'


class HTTPResponse:
    status = 200
    content = Content()
    request_info = Mock(real_url="https://aca.internal.example.azurecontainerapps.io/responses")
    history = ()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def test_aca_transport_is_stateless_and_does_not_forward_credentials():
    session = Mock()
    session.post.return_value = HTTPResponse()
    client = ACAClient(session, "https://aca.internal.example.azurecontainerapps.io")
    history = [{"role": "user", "content": "Books?"}]
    result = await client.responses.create(
        input=history, store=True, model="caller-model",
        extra_headers={"Authorization": "not-a-token", "x-client-comparison-id": "comparison"},
    )
    assert result.id == "resp_aca"
    args, kwargs = session.post.call_args
    assert args == ("https://aca.internal.example.azurecontainerapps.io/responses",)
    assert kwargs["json"] == {"input": history, "store": False, "stream": False}
    assert kwargs["headers"] == {"x-client-comparison-id": "comparison"}
    assert kwargs["allow_redirects"] is False


async def test_aca_redirect_is_rejected():
    response = HTTPResponse()
    response.status = 302
    session = Mock()
    session.post.return_value = response
    with pytest.raises(Exception, match="ACA runtime request failed"):
        await ACAClient(session, "https://aca.internal.example.azurecontainerapps.io").create(input=[])


@pytest.mark.parametrize("side", ["aca", "aca_none"])
async def test_aca_history_and_native_response_identity(side, raw_response):
    raw_response["metadata"] = {"native_response_id": "resp_model"}
    client = fake_client(raw_response)
    history = [{"role": "user", "content": "First"}, {"role": "assistant", "content": "Answer"}]
    result = await ComparisonService({side: client}, load_config()).compare(
        CompareRequest(message="Second", history={side: history}), "comparison",
    )
    assert result[side]["error"] is None
    assert result[side]["model_response_id"] == "resp_model"
    assert result[side]["continuation"] is None
    assert result[side]["conversation_scope"] == "aca_runtime"
    args = client.responses.create.call_args.kwargs
    assert args["input"] == history + [{"role": "user", "content": "Second"}]
    assert args["store"] is False
    assert "extra_body" not in args and "conversation" not in args
    client.conversations.create.assert_not_awaited()


@pytest.mark.parametrize("effort", ["low", "none"])
async def test_shared_runtime_direct_http_uses_foundry_search(settings, raw_response, monkeypatch, effort):
    monkeypatch.setenv("RUNTIME_SIDE", "aca" if effort == "low" else "aca_none")
    monkeypatch.setenv("REASONING_EFFORT_OVERRIDE", effort)
    client = fake_stream_client(raw_response)
    app = create_app(client, settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://aca") as transport:
            response = await transport.post("/responses", json={
                "input": [{"role": "user", "content": "Books?"}], "stream": False, "store": False,
                "model": "untrusted", "instructions": "untrusted", "reasoning": {"effort": "high"},
            })
    assert response.status_code == 200
    class RuntimeContent:
        async def iter_chunked(self, size):
            payload = response.json()
            payload.pop("tool_choice", None)
            yield json.dumps(payload).encode()

    upstream = HTTPResponse()
    upstream.content = RuntimeContent()
    session = Mock()
    session.post.return_value = upstream
    parsed = await ACAClient(session, "https://aca.internal.example.azurecontainerapps.io").create(input=[])
    assert parsed.model_dump(mode="json", exclude_unset=True, warnings=False)["output"] == response.json()["output"]
    assert parsed.model_dump(mode="json", exclude_unset=True, warnings=False)["usage"] == response.json()["usage"]
    assert response.json()["status"] == "completed"
    assert response.json()["metadata"]["native_response_id"] == raw_response["id"]
    args = client.responses.create.call_args.kwargs
    expected = model_options(settings, load_config())
    for key in ("model", "instructions", "tools", "reasoning", "store"):
        assert args[key] == expected[key]


async def test_clients_add_aca_without_foundry_registration(settings, monkeypatch):
    monkeypatch.setattr("comparison.clients.ManagedIdentityCredential", lambda **kwargs: Credential())
    extended = Settings(**{**settings.model_dump(),
        "aca_endpoint": "https://aca.internal.env.region.azurecontainerapps.io",
        "aca_endpoint_none": "https://aca-none.internal.env.region.azurecontainerapps.io",
    })
    async with AsyncExitStack() as stack:
        clients = await open_clients(stack, extended)
        assert set(clients) == {"prompt", "hosted", "aca", "aca_none"}
        assert isinstance(clients["aca"], ACAClient)


@pytest.mark.parametrize("endpoint", ["http://aca.internal.env.azurecontainerapps.io", "https://example.com", "https://aca.internal.env.azurecontainerapps.io/responses"])
def test_aca_endpoint_rejects_untrusted_targets(settings, endpoint):
    with pytest.raises(ValueError):
        Settings(**{**settings.model_dump(), "aca_endpoint": endpoint, "aca_endpoint_none": endpoint})


def test_aca_continuation_and_history_are_bounded():
    with pytest.raises(ValueError):
        CompareRequest(message="Q", continuation={"aca": "a" * 43})
    with pytest.raises(ValueError):
        CompareRequest(message="Q", history={"aca_none": [{"role": "user", "content": "a" * 12000}] * 3})


async def test_aca_transport_rejects_oversized_response():
    class OversizedContent:
        async def iter_chunked(self, size):
            for _ in range(MAX_RAW_BYTES // size + 1):
                yield b"x" * size

    response = HTTPResponse()
    response.content = OversizedContent()
    session = Mock()
    session.post.return_value = response
    with pytest.raises(ValueError, match="size limit"):
        await ACAClient(session, "https://aca.internal.env.azurecontainerapps.io").create(input=[])


async def test_aca_transport_cancellation_closes_response():
    started = asyncio.Event()
    closed = asyncio.Event()

    class WaitingResponse(HTTPResponse):
        async def __aexit__(self, *args):
            closed.set()

        async def iter_chunked(self, size):
            started.set()
            await asyncio.Event().wait()
            yield b""

    response = WaitingResponse()
    response.content = response
    session = Mock()
    session.post.return_value = response
    task = asyncio.create_task(ACAClient(session, "https://aca.internal.env.azurecontainerapps.io").create(input=[]))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_six_way_comparison_isolates_aca_failure(raw_response):
    clients = {side: fake_client(raw_response) for side in AGENT_SIDES}
    clients["aca_none"].responses.create.side_effect = TimeoutError()
    result = await ComparisonService(clients, load_config()).compare(CompareRequest(message="Books?"), "comparison")
    assert set(result) == set(AGENT_SIDES)
    assert result["aca_none"]["error"]
    assert all(result[side]["error"] is None for side in AGENT_SIDES if side != "aca_none")