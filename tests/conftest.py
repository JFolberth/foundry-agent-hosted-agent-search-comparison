import copy
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from comparison.config import Settings

_conversations = itertools.count(1)


@pytest.fixture
def settings():
    return Settings(
        project_endpoint="https://example.services.ai.azure.com/api/projects/comparison",
        model_deployment="gpt-5.6-terra",
        search_connection_id=(
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/"
            "providers/Microsoft.CognitiveServices/accounts/example/projects/comparison/connections/search"
        ),
        search_index="documents", prompt_agent="prompt-search", hosted_agent="hosted-search",
    )


@pytest.fixture
def raw_response():
    # Released 2.1.0 AzureAISearchToolCallOutput shape; output content is tool-specific.
    return {
        "id": "resp_native", "object": "response", "created_at": 1,
        "status": "completed", "model": "gpt-5.6-terra",
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "temperature": 1.0, "top_p": 1.0,
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "private-ciphertext"},
            {"type": "azure_ai_search_call_output", "id": "search_1", "call_id": "call_1", "status": "completed",
             "output": {"queries": ["What is indexed?"], "results": [{"id": "doc-1", "text": "Indexed source."}]}},
            {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "Indexed answer.",
                          "annotations": [{"type": "url_citation", "url": "https://example.org/doc",
                                           "title": "Source", "start_index": 0, "end_index": 7}]}]},
        ],
        "usage": {"input_tokens": 40, "output_tokens": 12, "total_tokens": 52,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 5}},
    }


def fake_client(raw):
    hosted_id = f"conv_hosted_model_{next(_conversations)}"
    async def respond(**kwargs):
        data = copy.deepcopy(raw)
        if not kwargs.get("store"):
            data.setdefault("metadata", {}).update(
                hosted_model_conversation_id=hosted_id,
                hosted_model_continuation="h" * 43,
                conversation_scope="hosted_model",
            )
        return SimpleNamespace(model_dump=lambda **kwargs: data)
    return SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock(side_effect=respond)),
        conversations=SimpleNamespace(create=AsyncMock(
            side_effect=lambda **kwargs: SimpleNamespace(id=f"conv_test_{next(_conversations)}"),
        )),
    )


def native_stream_events(raw):
    events = [{"type": "response.created", "response": {**raw, "status": "in_progress", "output": []}}]
    for index, item in enumerate(raw["output"]):
        for kind in ("response.output_item.added", "response.output_item.done"):
            events.append({
                "type": kind, "sequence_number": 999, "output_index": index,
                "item": {**item, "response_id": raw["id"]},
            })
    events.append({"type": f"response.{raw['status']}", "response": copy.deepcopy(raw)})
    return events


class FakeStream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def close(self):
        self.closed = True

    async def __aiter__(self):
        for event in self.events:
            yield copy.deepcopy(event)


def fake_stream_client(raw, events=None):
    async def create(**kwargs):
        return FakeStream(events if events is not None else native_stream_events(raw))
    return SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock(side_effect=create)),
        conversations=SimpleNamespace(create=AsyncMock(
            side_effect=lambda **kwargs: SimpleNamespace(id=f"conv_model_{next(_conversations)}"),
        )),
    )
