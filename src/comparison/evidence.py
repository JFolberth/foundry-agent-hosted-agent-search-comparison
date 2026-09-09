import re
import json
from urllib.parse import urlsplit

MAX_TEXT = 12000
MAX_EVIDENCE_CHARS = 32000
MAX_CALLS = 24
MAX_CITATIONS = 40
_PRIVATE_KEYS = {
    "authorization", "api_key", "apikey", "access_token", "secret", "password",
    "connection_string", "encrypted_content", "reasoning", "summary",
    "headers", "instructions", "system_prompt",
    "client_secret", "refresh_token", "id_token", "bearer_token",
    "continuation", "continuation_token",
    "hosted_model_continuation", "provider_token",
}


def clean_text(value: str, limit: int = MAX_TEXT) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [redacted]", value)
    value = re.sub(
        r"""(?i)\b(api[_-]?key|access[_-]?token|password|secret|AccountKey|SharedAccessSignature|encrypted_content|continuation)"""
        r"""(["']?\s*[=:]\s*["']?)[^\s,;"'}]+""", r"\1\2[redacted]", value,
    )
    return value[:limit]


def safe_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or len(value) > 2048
            or any(ord(c) < 33 for c in value)
        ):
            return None
        # Strip query strings/fragments (including SAS credentials) from clickable citations.
        return parsed._replace(query="", fragment="").geturl()
    except ValueError:
        return None


def sanitize(value, budget: list[int], depth=0):
    if depth > 8 or budget[0] <= 0:
        budget[1] = 1
        return "[truncated]"
    if isinstance(value, str):
        limit = min(budget[0], 8000)
        if value.startswith(("https://", "http://")):
            value = safe_url(value) or "[redacted URL]"
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                encoded = json.dumps(sanitize(parsed, budget, depth + 1), ensure_ascii=False)
                if len(encoded) > limit:
                    budget[1] = 1
                return encoded[:limit]
        if len(value) > limit:
            budget[1] = 1
        cleaned = clean_text(value, limit)
        budget[0] -= len(cleaned)
        return cleaned
    if isinstance(value, dict):
        if len(value) > 60:
            budget[1] = 1
        result = {}
        for key, item in list(value.items())[:60]:
            if key.lower().replace("-", "_") in _PRIVATE_KEYS:
                continue
            if key.lower() in ("url", "uri"):
                result[key] = safe_url(item) if isinstance(item, str) else None
            else:
                result[clean_text(str(key), 100)] = sanitize(item, budget, depth + 1)
        return result
    if isinstance(value, list):
        if len(value) > 40:
            budget[1] = 1
        return [sanitize(item, budget, depth + 1) for item in value[:40]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def _usage(raw):
    if not isinstance(raw, dict):
        return None
    allowed = ("input_tokens", "output_tokens", "total_tokens")
    result = {k: raw[k] for k in allowed if isinstance(raw.get(k), int) and raw[k] >= 0}
    for key in ("input_tokens_details", "output_tokens_details"):
        if isinstance(raw.get(key), dict):
            result[key] = {
                k: v for k, v in raw[key].items()
                if k in ("cached_tokens", "reasoning_tokens") and isinstance(v, int) and v >= 0
            }
    return result or None


def response_identifier(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", value):
        return value
    return None


def extract_evidence(raw: dict, *, hosted: bool = False) -> dict:
    text, calls, citations = [], [], []
    output = raw.get("output") or []
    budget = [MAX_EVIDENCE_CHARS, 0]
    for item in output:
        if not isinstance(item, dict):
            continue
        kind = item.get("type", "")
        # Reasoning items are retained only in private continuation state, never in analysis.
        if kind == "message" and item.get("role") == "assistant":
            for part in item.get("content") or []:
                if part.get("type") == "refusal":
                    text.append(part.get("refusal", ""))
                if part.get("type") == "output_text":
                    text.append(part.get("text", ""))
                    for annotation in part.get("annotations") or []:
                        if len(citations) < MAX_CITATIONS:
                            citation = sanitize(annotation, budget)
                            if not isinstance(citation, dict):
                                citation = {"evidence_truncated": True}
                                if isinstance(annotation, dict):
                                    for key in ("type", "file_id", "id", "title"):
                                        if isinstance(annotation.get(key), str):
                                            citation[key] = clean_text(annotation[key], 200)
                                    if isinstance(annotation.get("url"), str):
                                        citation["url"] = safe_url(annotation["url"])
                            citations.append(citation)
                        else:
                            budget[1] = 1
        elif isinstance(kind, str) and (
            kind.endswith("_call") or kind.endswith("_call_output")
            or kind in ("azure_ai_search", "tool_call", "tool_result")
        ):
            if len(calls) < MAX_CALLS:
                sanitized = sanitize(item, budget)
                if not isinstance(sanitized, dict):
                    sanitized = {"evidence_truncated": True}
                # Envelope IDs/types remain useful even when the document budget
                # is exhausted. Never replace an entire tool record with a string.
                for key in ("type", "id", "call_id", "status"):
                    if isinstance(item.get(key), str):
                        sanitized[key] = clean_text(item[key], 180)
                calls.append(sanitized)
            else:
                budget[1] = 1
    available = bool(calls)
    metadata = raw.get("metadata") if hosted and isinstance(raw.get("metadata"), dict) else {}
    return {
        "text": clean_text("\n".join(text)),
        "error": None,
        "usage": _usage(raw.get("usage")),
        "tool_calls": calls,
        "tool_evidence_available": available,
        "tool_evidence_note": (
            "Observed native output items only; completeness and per-tool timing are not guaranteed."
            if available else
            "Native tool-call items were not exposed. This does not mean zero calls. "
            "Use the comparison ID in Application Insights; citations are not tool-call counts."
        ),
        "tool_evidence_truncated": bool(budget[1]),
        "citations": citations,
        "response_id": response_identifier(raw.get("id")),
        "model_response_id": response_identifier(metadata.get("native_response_id")),
        "output_truncated": len("\n".join(text)) > MAX_TEXT,
    }


def error_result(message: str) -> dict:
    result = extract_evidence({})
    result.update(
        error=message, latency_ms=None, continuation=None, trace_id=None, span_id=None,
        conversation_id=None, conversation_note="No provider conversation ID is available.",
        hosted_runtime_trace_id=None, hosted_runtime_span_id=None,
        hosted_runtime_telemetry_note="No hosted runtime telemetry IDs were returned.",
    )
    return result
