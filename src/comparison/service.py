import asyncio
import re
import time

from opentelemetry.propagate import inject

from .agents import kind_of
from .contracts import CompareRequest, MAX_TURNS, REQUEST_TIMEOUT
from .diagnostics import failure, from_metadata, record_failure
from .evidence import error_result, extract_evidence
from .state import HistoryExpired, HistoryFull, HistoryStore, validate_raw
from .telemetry import record_evidence, telemetry_ids, tracer


def provider_id(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
        return value
    return None


def runtime_telemetry(raw, kind):
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    values = {}
    for key, length in (("trace_id", 32), ("span_id", 16)):
        name = f"hosted_runtime_{key}"
        value = metadata.get(name)
        values[name] = (
            value if kind == "hosted" and isinstance(value, str)
            and re.fullmatch(f"[0-9a-f]{{{length}}}", value) and int(value, 16) else None
        )
    if not all(values.values()):
        values = {key: None for key in values}
    values["hosted_runtime_telemetry_note"] = (
        "Recorded hosted runtime model span; distinct from the UI invocation span."
        if all(values.values()) else "No recorded hosted runtime telemetry IDs were returned."
    )
    return values


class ConversationUnavailable(Exception):
    pass


class ComparisonService:
    def __init__(self, clients, config, store=None, timeout=REQUEST_TIMEOUT):
        self.clients = clients
        self.config = config
        self.store = store or HistoryStore()
        self.timeout = timeout

    async def compare(self, request: CompareRequest, comparison_id: str):
        sides = tuple(self.clients)
        results = await asyncio.gather(*(
            self._one(side, request, comparison_id) for side in sides
        ))
        return dict(zip(sides, results))

    async def _one(self, side, request, comparison_id):
        started = time.perf_counter()
        conversation_id = None
        stage = "request.validate"
        diagnostic = None
        kind = kind_of(side)
        with tracer.start_as_current_span(
            f"agent.{side}", record_exception=False, set_status_on_exception=False,
            attributes={"comparison.id": comparison_id, "comparison.side": side},
        ) as span:
            try:
                token = getattr(request.continuation, side)
                seed = []
                if kind == "hosted":
                    seed = [m.model_dump() for m in getattr(request.history, side)]
                    turns = sum(item["role"] == "user" for item in seed)
                elif token:
                    stage = "continuation"
                    # A Foundry conversation is mutable. Consuming the capability
                    # before awaiting prevents concurrent reuse and stale replay.
                    snapshot = self.store.take(token, side)
                    conversation_id, turns = snapshot.conversation_id, snapshot.turns
                    if not conversation_id:
                        raise HistoryExpired()
                else:
                    seed = [m.model_dump() for m in getattr(request.history, side)]
                    turns = sum(item["role"] == "user" for item in seed)
                if turns >= MAX_TURNS:
                    raise HistoryFull()
                validate_raw(seed)
                carrier = {}
                inject(carrier)
                headers = {"x-client-comparison-id": comparison_id}
                if carrier.get("traceparent"):
                    headers["x-client-traceparent"] = carrier["traceparent"]
                async with asyncio.timeout(self.timeout):
                    if kind == "prompt" and conversation_id is None:
                        stage = "prompt.conversations.create"
                        try:
                            conversation = await self.clients[side].conversations.create(
                                items=seed, extra_headers=dict(headers),
                            )
                        except Exception as exc:
                            raise ConversationUnavailable() from exc
                        conversation_id = provider_id(conversation.id)
                        if conversation_id is None:
                            raise ConversationUnavailable()
                    invocation = {"conversation": conversation_id} if kind == "prompt" else {}
                    stage = f"{side}.responses.create"
                    response = await self.clients[side].responses.create(
                        input=(seed if kind == "hosted" else []) + [{"role": "user", "content": request.message}],
                        max_output_tokens=self.config.max_output_tokens,
                        include=["reasoning.encrypted_content"],
                        store=kind == "prompt", stream=False, extra_headers=dict(headers), **invocation,
                    )
                stage = f"{side}.response.parse"
                raw = response.model_dump(mode="json", exclude_unset=True, warnings=False)
                reported = raw.get("conversation")
                reported = reported.get("id") if isinstance(reported, dict) else reported
                if kind == "hosted":
                    reported = provider_id(reported)
                    conversation_id = reported
                if reported is not None and reported != conversation_id:
                    result = error_result("Agent returned a different conversation. Reset before continuing.")
                else:
                    result = extract_evidence(raw, hosted=kind == "hosted")
                    result.update(runtime_telemetry(raw, kind))
                    if raw.get("status") != "completed":
                        result["error"] = "Agent response was incomplete or failed. Reset this conversation before retrying."
                        result["continuation"] = None
                        diagnostic = (
                            from_metadata(raw) if kind == "hosted" else None
                        ) or failure(
                            stage, kind="UpstreamIncomplete" if raw.get("status") == "incomplete" else "UpstreamFailed",
                            request=getattr(response, "_request_id", None),
                        )
                    elif (kind == "prompt" and conversation_id is None) or (
                        kind == "hosted" and result["response_id"] is None
                    ):
                        result["error"] = "Agent did not return a usable provider conversation continuation. Reset to continue."
                        result["continuation"] = None
                    else:
                        result["continuation"] = (
                            self.store.put(side, [], turns + 1, conversation_id=conversation_id)
                            if kind == "prompt" else None
                        )
                record_evidence(span, result, comparison_id)
            except ConversationUnavailable as exc:
                diagnostic = failure(stage, exc.__cause__ if exc.__cause__ is not None else exc)
                result = error_result(
                    "Could not create a Foundry conversation on this agent route. Check API support and permissions."
                )
            except HistoryExpired as exc:
                diagnostic = failure(stage, exc)
                result = error_result("Conversation expired, was already used, or belongs to another side. Reset to continue.")
            except HistoryFull as exc:
                diagnostic = failure(stage, exc)
                result = error_result("Conversation limit reached. Reset to continue.")
            except TimeoutError as exc:
                diagnostic = failure(stage, exc)
                result = error_result("Agent timed out. Reset this conversation before retrying.")
            except asyncio.CancelledError as exc:
                record_failure(span, failure(stage, exc), comparison_id)
                raise
            except Exception as exc:
                diagnostic = failure(stage, exc)
                result = error_result("Agent request failed. Reset this conversation and check the comparison trace.")
            result["conversation_id"] = conversation_id
            result["conversation_scope"] = "hosted_agent" if kind == "hosted" else "agent"
            if conversation_id:
                span.set_attribute("gen_ai.conversation.id", conversation_id)
            result["conversation_note"] = (
                ("Provider-reported hosted conversation ID; history is supplied explicitly with each request."
                 if kind == "hosted" else "Actual agent-bound Foundry conversation; provider-managed history.")
                if conversation_id else (
                    "Hosted user/assistant history is resent with each request. Responses are not stored; no conversation ID was reported."
                    if kind == "hosted" else "No provider conversation ID is available; none was fabricated."
                )
            )
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            result["error_diagnostics"] = None
            if result["error"] is not None:
                diagnostic = diagnostic or failure(stage, kind="ValueError")
                if diagnostic["type"] == "RateLimitError":
                    result["error"] = "Model deployment rate limit reached. Wait before starting a new comparison."
                result["error_diagnostics"] = diagnostic
                record_failure(span, diagnostic, comparison_id)
            result.update(telemetry_ids())
            span.set_attribute("comparison.success", result["error"] is None)
            return result
