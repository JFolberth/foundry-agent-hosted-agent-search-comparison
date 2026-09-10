import json
import pytest

from comparison.evidence import extract_evidence, safe_url, strip_citation_markers


def test_model_response_id_is_separate_from_the_invoked_agent_response():
    envelope = {"id": "resp_outer", "metadata": {"native_response_id": "resp_model"}}
    hosted = extract_evidence(envelope, hosted=True)
    assert hosted["response_id"] == "resp_outer"
    assert hosted["model_response_id"] == "resp_model"
    assert extract_evidence(envelope)["model_response_id"] is None


@pytest.mark.parametrize("value", [
    None, "", 42, True, {"id": "resp_model"}, ["resp_model"],
    "<script>alert(1)</script>", "resp_model\n", "Bearer private", "x" * 181,
])
def test_invalid_native_response_id_is_null_without_coercion_or_fallback(value):
    result = extract_evidence({"id": "resp_outer", "metadata": {"native_response_id": value}}, hosted=True)
    assert result["response_id"] == "resp_outer"
    assert result["model_response_id"] is None


@pytest.mark.parametrize("metadata", [None, "resp_model", [], {}])
def test_missing_or_invalid_metadata_never_invents_a_model_response_id(metadata):
    result = extract_evidence({"id": "resp_outer", "metadata": metadata}, hosted=True)
    assert result["response_id"] == "resp_outer"
    assert result["model_response_id"] is None


def test_real_evidence_usage_ids_citations_preserved(raw_response):
    result = extract_evidence(raw_response)
    assert result["tool_evidence_available"]
    assert result["tool_calls"][0]["id"] == "search_1"
    assert result["tool_calls"][0]["output"]["results"][0]["text"] == "Indexed source."
    assert result["usage"]["output_tokens_details"]["reasoning_tokens"] == 5
    assert result["citations"][0]["title"] == "Source"
    assert result["response_id"] == "resp_native"
    assert "private-ciphertext" not in json.dumps(result)


def test_inline_citation_markers_are_stripped_from_displayed_text(raw_response):
    raw_response["output"][-1]["content"][0]["text"] = (
        "GrandPre\u30104:0\u2020source\u3011 recommends this book\u301012:3\u2020source\u3011."
    )
    result = extract_evidence(raw_response)
    assert "\u3010" not in result["text"] and "\u3011" not in result["text"]
    assert result["text"] == "GrandPre recommends this book."
    # The structured citation is still captured separately for programmatic use.
    assert result["citations"][0]["title"] == "Source"


@pytest.mark.parametrize("value,expected", [
    ("Plain text with no markers.", "Plain text with no markers."),
    ("Before\u30105:1\u2020source\u3011after", "Beforeafter"),
    ("\u3010unterminated", "\u3010unterminated"),
])
def test_strip_citation_markers_handles_edge_cases(value, expected):
    assert strip_citation_markers(value) == expected


def test_missing_telemetry_is_unknown_not_zero(raw_response):
    raw_response["output"] = [raw_response["output"][-1]]
    raw_response.pop("usage")
    result = extract_evidence(raw_response)
    assert not result["tool_evidence_available"]
    assert result["tool_calls"] == []
    assert result["usage"] is None
    assert len(result["citations"]) == 1
    assert "does not mean zero" in result["tool_evidence_note"]


def test_sanitization_output_limits_and_unsafe_links(raw_response):
    raw_response["output"][1]["headers"] = {"Authorization": "Bearer private"}
    raw_response["output"][1]["output"]["results"][0]["text"] = "secret=unsafe\x00"
    raw_response["output"][1]["output"]["results"][0]["url"] = "javascript:alert(1)"
    raw_response["output"][-1]["content"][0]["text"] = "x" * 20000
    result = extract_evidence(raw_response)
    assert result["output_truncated"]
    assert len(result["text"]) == 12000
    assert "unsafe" not in json.dumps(result)
    assert "Authorization" not in json.dumps(result)
    assert result["tool_calls"][0]["output"]["results"][0]["url"] is None
    assert safe_url("https://example.org/doc?sig=private#fragment") == "https://example.org/doc"
    assert safe_url("https://user:password@example.org") is None
    assert safe_url("data:text/html,evil") is None


def test_refusal_is_visible_without_reasoning(raw_response):
    raw_response["output"][-1]["content"] = [{
        "type": "refusal", "refusal": "I cannot help with that request.",
    }]
    result = extract_evidence(raw_response)
    assert result["text"] == "I cannot help with that request."
    assert "private-ciphertext" not in json.dumps(result)


def test_nested_native_output_truncation_is_reported(raw_response):
    raw_response["output"][1]["output"]["results"][0]["text"] = "x" * 9000
    result = extract_evidence(raw_response)
    assert result["tool_evidence_truncated"]
    assert len(result["tool_calls"][0]["output"]["results"][0]["text"]) == 8000


def test_json_string_tool_output_cannot_expose_credentials_or_continuations(raw_response):
    raw_response["output"][1]["output"] = json.dumps({
        "api_key": "credential-not-for-display",
        "encrypted_content": "encrypted-reasoning-not-for-display",
        "continuation": "opaque-capability-not-for-display",
        "results": [{"text": "Visible indexed content.", "Authorization": "Bearer private"}],
    })
    result = extract_evidence(raw_response)
    rendered = json.dumps(result)
    assert "credential-not-for-display" not in rendered
    assert "encrypted-reasoning-not-for-display" not in rendered
    assert "opaque-capability-not-for-display" not in rendered
    assert "Bearer private" not in rendered
    assert "Visible indexed content." in rendered
