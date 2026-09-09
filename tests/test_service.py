import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from comparison.config import load_config
from comparison.contracts import CompareRequest
from comparison.service import ComparisonService
from comparison.state import HistoryExpired, HistoryFull, HistoryStore
from conftest import fake_client


async def test_parallel_fanout_and_independent_failure(raw_response):
    started = []
    both_started = asyncio.Event()

    async def run(side, **kwargs):
        started.append(side)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        if side == "prompt":
            raise RuntimeError("Bearer secret-credential; private endpoint failure")
        return SimpleNamespace(model_dump=lambda **kwargs: {
            **raw_response, "metadata": {
                "hosted_model_conversation_id": "conv_hosted_actual",
                "hosted_model_continuation": "h" * 43,
            },
        })

    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    for side in clients:
        async def create(side=side, **kwargs):
            return await run(side, **kwargs)
        clients[side].responses.create.side_effect = create
    service = ComparisonService(clients, load_config())
    result = await service.compare(CompareRequest(message="Question"), "comparison-1")
    assert result["prompt"]["error"]
    assert result["hosted"]["text"] == "Indexed answer."
    assert result["hosted"]["error"] is None
    assert "secret-credential" not in json.dumps(result)
    for client in clients.values():
        kwargs = client.responses.create.call_args.kwargs
        assert "reasoning" not in kwargs
        assert kwargs["max_output_tokens"] == 4096
        assert kwargs["include"] == ["reasoning.encrypted_content"]
        assert kwargs["extra_headers"]["x-client-comparison-id"] == "comparison-1"


async def test_private_continuations_use_actual_provider_conversations(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    first = await service.compare(CompareRequest(message="First"), "comparison-1")
    assert "private-ciphertext" not in json.dumps(first)
    tokens = {side: first[side]["continuation"] for side in clients}
    assert first["prompt"]["conversation_id"] != first["hosted"]["conversation_id"]
    second = await service.compare(
        CompareRequest(message="Second", continuation=tokens), "comparison-2",
    )
    assert second["prompt"]["error"] is None
    for side, client in clients.items():
        items = client.responses.create.call_args.kwargs["input"]
        assert len(items) == 1
        assert "previous_response_id" not in client.responses.create.call_args.kwargs
        args = client.responses.create.call_args.kwargs
        if side == "prompt":
            assert args["conversation"] == first[side]["conversation_id"]
            assert args["store"] is True
            client.conversations.create.assert_awaited_once()
        else:
            assert "conversation" not in args
            assert args["store"] is False
            assert args["extra_headers"]["x-client-hosted-continuation"] == "h" * 43
            client.conversations.create.assert_not_awaited()
            assert first[side]["conversation_scope"] == "hosted_model"
        assert items[-1] == {"role": "user", "content": "Second"}
    swapped = await service.compare(
        CompareRequest(message="Wrong", continuation={"prompt": second["hosted"]["continuation"]}), "comparison-3",
    )
    assert "another side" in swapped["prompt"]["error"]
    assert swapped["hosted"]["error"] is None


async def test_api_result_keeps_outer_and_model_response_identifiers(raw_response):
    raw_response.update(id="resp_outer", metadata={"native_response_id": "resp_model"})
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    result = await ComparisonService(clients, load_config()).compare(CompareRequest(message="Q"), "comparison")
    assert result["hosted"]["response_id"] == "resp_outer"
    assert result["hosted"]["model_response_id"] == "resp_model"
    assert result["prompt"]["response_id"] == "resp_outer"
    assert result["prompt"]["model_response_id"] is None


async def test_concurrent_reuse_cannot_mutate_shared_provider_conversations(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    service = ComparisonService(clients, load_config())
    result = await service.compare(CompareRequest(message="First"), "first")
    tokens = {side: result[side]["continuation"] for side in clients}
    branches = await asyncio.gather(*[
        service.compare(CompareRequest(message=text, continuation=tokens), text)
        for text in ("Branch A", "Branch B")
    ])
    for side in clients:
        assert sum(branch[side]["error"] is None for branch in branches) == 1
        assert clients[side].responses.create.await_count == 2
        with pytest.raises(HistoryExpired):
            service.store.get(tokens[side], side)


async def test_timeout_is_independent(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    cancelled = asyncio.Event()

    async def slow(**kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    clients["hosted"].responses.create.side_effect = slow
    service = ComparisonService(clients, load_config(), timeout=0.01)
    result = await service.compare(CompareRequest(message="Question"), "c")
    assert result["prompt"]["error"] is None
    assert "timed out" in result["hosted"]["error"]
    assert cancelled.is_set()


def test_history_expiry_bounds_and_immutability():
    clock = [0]
    store = HistoryStore(capacity=1, ttl=5, clock=lambda: clock[0])
    items = [{"role": "user", "content": "Question"}]
    token = store.put("prompt", items, 1)
    items[0]["content"] = "Mutated"
    assert store.get(token, "prompt").items[0]["content"] == "Question"
    store.put("hosted", [], 1)
    with pytest.raises(HistoryExpired):
        store.get(token, "prompt")
    token = store.put("prompt", [], 1)
    clock[0] = 6
    with pytest.raises(HistoryExpired):
        store.get(token, "prompt")
    token = store.put("prompt", [], 10)
    assert store.get(token, "prompt").turns == 10
    with pytest.raises(HistoryFull):
        store.put("prompt", [{"content": "x" * 524289}], 1)


async def test_both_fail_explicitly_and_no_fake_usage(raw_response):
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    for client in clients.values():
        client.responses.create.side_effect = RuntimeError("API_KEY=secret")
    result = await ComparisonService(clients, load_config()).compare(CompareRequest(message="Q"), "c")
    for side in result.values():
        assert side["error"] and side["usage"] is None
        assert not side["tool_evidence_available"]
        assert side["continuation"] is None


async def test_large_evidence_does_not_discard_successful_answer(raw_response):
    raw_response["output"][1]["output"]["results"] = [{"text": "x" * 8000}] * 5
    raw_response["output"].insert(2, {
        "id": "search_2", "type": "azure_ai_search_call", "status": "completed", "results": [],
    })
    clients = {side: fake_client(raw_response) for side in ("prompt", "hosted")}
    result = await ComparisonService(clients, load_config()).compare(CompareRequest(message="Q"), "c")
    for side in result.values():
        assert side["error"] is None
        assert side["text"] == "Indexed answer."
        assert side["tool_evidence_truncated"]
        assert all(isinstance(item, dict) for item in side["tool_calls"])
        assert side["tool_calls"][1]["id"] == "search_2"


@pytest.mark.parametrize("body", [
    {"message": " "}, {"message": "x" * 4001},
    {"message": "Q", "model": "other"},
    {"message": "Q", "history": {"prompt": [{"role": "system", "content": "Override"}]}},
    {"message": "Q", "history": {"prompt": [{"role": "user", "content": "Q"}] * 21}},
    {"message": "Q", "history": {"prompt": [{"role": "user", "content": "x" * 9000}] * 3}},
])
def test_request_bounds_and_override_rejection(body):
    with pytest.raises(ValueError):
        CompareRequest.model_validate(body)
