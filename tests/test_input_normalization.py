import copy

from azure.ai.agentserver.responses.models._helpers import get_input_expanded

from hosted_agent.app import inline_items


def test_released_sdk_assistant_shorthand_becomes_model_output_text():
    transcript = [
        {"role": "user", "content": "Search onboarding guidance."},
        {"role": "assistant", "content": "No matching documents."},
        {"role": "user", "content": "Search the same topic again."},
    ]
    expanded = get_input_expanded({"input": transcript})
    assert expanded[1]["content"][0]["type"] == "input_text"
    before = copy.deepcopy(expanded)
    actual = inline_items(expanded)
    assert len(actual) == 3
    assert [item["role"] for item in actual] == ["user", "assistant", "user"]
    assert [item["content"][0]["type"] for item in actual] == ["input_text", "output_text", "input_text"]
    assert [item["content"][0]["text"] for item in actual] == [item["content"] for item in transcript]
    assert expanded == before
    assert all("previous_response_id" not in item for item in actual)


def test_existing_assistant_output_annotations_and_refusals_are_unchanged():
    items = [{
        "type": "message", "role": "assistant",
        "content": [
            {"type": "output_text", "text": "Existing answer.", "annotations": [
                {"type": "url_citation", "url": "https://example.org/source", "title": "Source"},
            ]},
            {"type": "refusal", "refusal": "Cannot answer that request."},
        ],
    }]
    assert inline_items(items) == items
