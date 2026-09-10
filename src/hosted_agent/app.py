import asyncio
import copy
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID, uuid4

from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.responses import (
    CreateResponse, InMemoryResponseProvider, ResponseContext, ResponsesAgentServerHost, ResponsesServerOptions,
)
from opentelemetry.propagate import extract
from starlette.responses import JSONResponse
from starlette.routing import Route

from comparison.clients import open_clients
from comparison.config import Settings, load_config, model_options
from comparison.contracts import MAX_RAW_BYTES, MAX_RAW_ITEMS, MAX_TURNS, MODEL_TIMEOUT
from comparison.diagnostics import failure, from_metadata, http_status, record_failure, response_request_id, to_metadata
from comparison.evidence import extract_evidence, response_identifier
from comparison.guard import HostedRequestGuard, RequestGuard
from comparison.state import HistoryFull, validate_raw
from comparison.telemetry import configure_telemetry, record_evidence, telemetry_ids, tracer


def correlation_id(headers: dict) -> str:
    try:
        return str(UUID(headers.get("x-client-comparison-id", "")))
    except (ValueError, TypeError):
        return str(uuid4())


def inline_items(items):
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    result = [copy.deepcopy(dict(item)) for item in items]
    validate_raw(result)
    for item in result:
        if item.get("type", "message") not in (
            "message", "reasoning", "azure_ai_search_call", "azure_ai_search_call_output",
        ):
            raise ValueError("Only native text, reasoning, and Search history is supported")
        if item.get("role") and item["role"] not in ("user", "assistant"):
            raise ValueError("Only user and assistant messages are supported")
        content = item.get("content")
        if item.get("role") and isinstance(content, list):
            allowed = ("input_text", "output_text", "refusal") if item["role"] == "assistant" else ("input_text",)
            if any(part.get("type") not in allowed for part in content):
                raise ValueError("Only text content is supported")
            if item["role"] == "assistant":
                # AgentServer expands all string content as input_text, but the
                # model Responses API requires output_text for assistant history.
                for part in content:
                    if part["type"] == "input_text":
                        part["type"] = "output_text"
        # Hosted response ownership is distinct from the model's response store.
        item.pop("response_id", None)
        item.pop("agent_reference", None)
    if sum(item.get("role") == "user" for item in result) > MAX_TURNS:
        raise HistoryFull()
    return result


def wire_dict(value):
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return value.model_dump(mode="json", exclude_unset=True, warnings=False)


def unstamp(value):
    if isinstance(value, list):
        return [unstamp(item) for item in value]
    result = copy.deepcopy(value)
    if isinstance(result, dict):
        result.pop("response_id", None)
        if isinstance(result.get("item"), dict):
            result["item"].pop("response_id", None)
        if result.get("object") == "response" and isinstance(result.get("output"), list):
            result["output"] = unstamp(result["output"])
    return result


class InterruptedStream(Exception):
    pass


class CancelledStream(Exception):
    pass


async def interruptible(awaitable, cancellation_signal, shutdown, *, acquiring=False):
    operation = asyncio.ensure_future(awaitable)
    cancel = asyncio.create_task(cancellation_signal.wait())
    stopping = asyncio.create_task(shutdown.wait())
    delivered = False

    async def cleanup():
        for task in (operation, cancel, stopping):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, cancel, stopping, return_exceptions=True)

    try:
        done, _ = await asyncio.wait((operation, cancel, stopping), return_when=asyncio.FIRST_COMPLETED)
        if cancel in done or stopping in done:
            raise CancelledStream()
        result = await operation
        await cleanup()
        # No await after the ownership transfer: cancellation during cleanup
        # must still close the stream instead of losing it between contexts.
        delivered = True
        return result
    finally:
        if not delivered:
            await cleanup()
            if acquiring and not operation.cancelled() and operation.exception() is None:
                await operation.result().close()


def accumulate_partial(items, event):
    index = event.get("output_index")
    if index is None and event.get("call_id"):
        index = next((i for i, item in items.items() if item.get("call_id") == event["call_id"]), None)
    if index not in items:
        return
    item = items[index]
    kind = event["type"]
    content_index = event.get("content_index", 0)
    if not isinstance(content_index, int) or not 0 <= content_index < MAX_RAW_ITEMS:
        raise InterruptedStream()
    if kind in ("response.content_part.added", "response.content_part.done"):
        content = item.setdefault("content", [])
        while len(content) <= content_index:
            content.append({})
        content[content_index] = copy.deepcopy(event["part"])
    elif kind.startswith(("response.output_text.", "response.refusal.")):
        content = item.setdefault("content", [])
        while len(content) <= content_index:
            content.append({})
        part = content[content_index]
        field = "refusal" if kind.startswith("response.refusal.") else "text"
        part.setdefault("type", "refusal" if field == "refusal" else "output_text")
        if kind.endswith(".delta"):
            part[field] = part.get(field, "") + event.get("delta", "")
        elif kind.endswith(".done"):
            part[field] = event.get(field, part.get(field, ""))
        elif "annotation" in kind and isinstance(event.get("annotation"), dict):
            annotations = part.setdefault("annotations", [])
            annotation_index = event.get("annotation_index", len(annotations))
            if not isinstance(annotation_index, int) or not 0 <= annotation_index < MAX_RAW_ITEMS:
                raise InterruptedStream()
            while len(annotations) <= annotation_index:
                annotations.append({})
            annotations[annotation_index] = copy.deepcopy(event["annotation"])
    else:
        # Unknown native/ reasoning deltas are retained as actual events rather
        # than guessed output. A later item.done replaces this partial snapshot.
        item.setdefault("partial_events", []).append(copy.deepcopy(event))
    validate_raw(list(items.values()))


def snapshot(request, context, settings):
    result = {
        "id": context.response_id, "object": "response", "status": "in_progress",
        "model": settings.model_deployment, "output": [], "store": False,
        "created_at": int(context.created_at.timestamp()),
    }
    return result


async def stream_response(client, settings, config, request, context, cancellation_signal):
    """Released SDK sample_10 pattern: our lifecycle, native upstream content events."""
    envelope = snapshot(request, context, settings)
    yield {"type": "response.created", "response": copy.deepcopy(envelope)}
    yield {"type": "response.in_progress", "response": copy.deepcopy(envelope)}
    seen, done_items = {}, {}
    terminal = None
    total_bytes = 0
    stage = "hosted.model.input"
    diagnostic = None
    upstream_status = None
    upstream_request_id = None
    try:
        async with asyncio.timeout(MODEL_TIMEOUT):
            if request.get("store") is True or request.get("conversation") or request.get("previous_response_id"):
                raise ValueError("Hosted requests must supply inline history without persistence")
            current = await interruptible(context.get_input_items(), cancellation_signal, context.shutdown)
            items = inline_items(current)
            if not items or items[-1].get("role") != "user":
                raise ValueError("A current user message is required")
            headers = {"x-client-comparison-id": correlation_id(context.client_headers)}
            identity = get_request_context()
            headers.update(identity.platform_headers())
            options = model_options(settings, config)
            stage = "hosted.model.responses.create"
            upstream = await interruptible(
                client.responses.create(
                    input=items, stream=True, extra_headers=dict(headers), **options,
                ), cancellation_signal, context.shutdown, acquiring=True,
            )
            async with upstream:
                response = getattr(upstream, "response", None)
                if response is not None:
                    upstream_status = http_status(getattr(response, "status_code", None))
                    upstream_request_id = response_request_id(response)
                stage = "hosted.model.stream"
                iterator = upstream.__aiter__()
                while True:
                    try:
                        event = wire_dict(await interruptible(anext(iterator), cancellation_signal, context.shutdown))
                    except StopAsyncIteration:
                        break
                    total_bytes += len(json.dumps(event).encode())
                    if total_bytes > 4 * MAX_RAW_BYTES:
                        raise InterruptedStream()
                    kind = event.get("type", "")
                    if kind in ("response.created", "response.in_progress", "response.queued"):
                        continue
                    if kind in ("response.completed", "response.failed", "response.incomplete"):
                        terminal = unstamp(event["response"])
                        status = terminal.get("status")
                        expected = {
                            "response.completed": ("completed",),
                            "response.incomplete": ("incomplete",),
                            "response.failed": ("failed", "cancelled"),
                        }
                        if status not in expected[kind]:
                            raise InterruptedStream()
                        break
                    if kind in ("error", "response.error"):
                        raise InterruptedStream()
                    event.pop("sequence_number", None)
                    event = unstamp(event)
                    if kind in ("response.output_item.added", "response.output_item.done"):
                        index = event["output_index"]
                        if not isinstance(index, int) or not 0 <= index < MAX_RAW_ITEMS:
                            raise InterruptedStream()
                        if kind.endswith(".added"):
                            seen[index] = copy.deepcopy(event["item"])
                        else:
                            if index not in seen:
                                raise InterruptedStream()
                            done_items[index] = event["item"]
                    else:
                        accumulate_partial(seen, event)
                    yield event
            if terminal is None:
                raise InterruptedStream()
            stage = "hosted.model.response.validate"
            output = terminal.get("output")
            if not isinstance(output, list):
                raise InterruptedStream()
            validate_raw(output)
            # Some native tools appear only in the terminal snapshot. Represent
            # those real items in the host lifecycle without inventing any calls.
            for index, item in enumerate(output):
                if index not in seen:
                    seen[index] = item
                    yield {"type": "response.output_item.added", "output_index": index, "item": item}
                elif seen[index].get("id") != item.get("id"):
                    raise InterruptedStream()
                if index not in done_items:
                    done_items[index] = item
                    yield {"type": "response.output_item.done", "output_index": index, "item": item}
            if len(seen) != len(output):
                raise InterruptedStream()
            for key, value in terminal.items():
                if key not in ("id", "object", "created_at", "previous_response_id", "conversation", "store", "metadata"):
                    envelope[key] = value
            native_id = response_identifier(terminal.get("id"))
            if native_id is not None:
                envelope.setdefault("metadata", {})["native_response_id"] = native_id
            if envelope["status"] != "completed":
                diagnostic = failure(
                    "hosted.model.stream",
                    kind="UpstreamIncomplete" if envelope["status"] == "incomplete" else "UpstreamFailed",
                    status=upstream_status, request=upstream_request_id,
                )
    except CancelledStream as exc:
        diagnostic = failure(stage, exc)
        envelope.update(status="cancelled", error={"code": "cancelled", "message": "Request cancelled."})
    except (TimeoutError, InterruptedStream, HistoryFull) as exc:
        diagnostic = failure(stage, exc)
        envelope.update(
            status="incomplete", incomplete_details={"reason": "upstream_interrupted"},
            error={"code": "upstream_interrupted", "message": "Model stream or history limit interrupted the response."},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        diagnostic = failure(stage, exc)
        envelope.update(status="failed", error={"code": "server_error", "message": "Hosted model request failed."})
    if diagnostic:
        diagnostic["request_id"] = diagnostic["request_id"] or upstream_request_id
        envelope.setdefault("metadata", {}).update(to_metadata(diagnostic))
    if terminal is None or envelope["status"] not in ("completed", "incomplete", "failed", "cancelled"):
        envelope["output"] = [done_items.get(i, seen[i]) for i in sorted(seen)]
    elif "output" not in envelope or not envelope["output"]:
        envelope["output"] = [done_items.get(i, seen[i]) for i in sorted(seen)]
    kind = "response.failed" if envelope["status"] == "cancelled" else f"response.{envelope['status']}"
    yield {"type": kind, "response": envelope}


def create_app(client=None, settings=None, store=None):
    config = load_config()
    runtime = {"client": client, "settings": settings, "ready": client is not None}

    @asynccontextmanager
    async def lifespan(app):
        configure_telemetry("comparison-hosted")
        async with AsyncExitStack() as stack:
            runtime["settings"] = runtime["settings"] or Settings.from_env()
            if runtime["client"] is None:
                runtime["client"] = await open_clients(stack, runtime["settings"], hosted=True)
            async with original_lifespan(app):
                runtime["ready"] = True
                try:
                    yield
                finally:
                    runtime["ready"] = False

    async def health(request):
        return JSONResponse(
            {"status": "ready" if runtime["ready"] else "starting"},
            status_code=200 if runtime["ready"] else 503,
        )

    os.environ.setdefault("AGENTSERVER_STATE_ROOT", ".runtime")
    host = ResponsesAgentServerHost(
        options=ResponsesServerOptions(default_fetch_history_count=MAX_RAW_ITEMS + 1),
        store=store if store is not None else InMemoryResponseProvider(), configure_observability=None, access_log=None,
        routes=[Route(path, health, methods=["GET"]) for path in ("/readiness", "/health", "/health/readiness")],
    )
    original_lifespan = host.router.lifespan_context
    host.router.lifespan_context = lifespan

    @host.response_handler
    async def handler(request: CreateResponse, context: ResponseContext, cancellation_signal: asyncio.Event):
        comparison_id = correlation_id(context.client_headers)
        carrier = {"traceparent": context.client_headers.get("x-client-traceparent", "")}
        with tracer.start_as_current_span(
            "hosted.model", context=extract(carrier),
            record_exception=False, set_status_on_exception=False,
            attributes={"comparison.id": comparison_id, "comparison.side": "hosted"},
        ) as span:
            try:
                async for event in stream_response(
                    runtime["client"], runtime["settings"], config, request, context, cancellation_signal,
                ):
                    if event["type"] in ("response.completed", "response.failed", "response.incomplete"):
                        ids = telemetry_ids()
                        metadata = event["response"].setdefault("metadata", {})
                        for key in ("trace_id", "span_id"):
                            if ids[key]:
                                metadata[f"hosted_runtime_{key}"] = ids[key]
                        if context.conversation_id:
                            span.set_attribute("gen_ai.conversation.id", context.conversation_id)
                        model_evidence = extract_evidence(event["response"], hosted=True)
                        model_evidence["response_id"] = model_evidence["model_response_id"]
                        record_evidence(span, model_evidence, comparison_id)
                        if event["response"].get("status") != "completed":
                            diagnostic = from_metadata(event["response"]) or failure("hosted.model.stream", kind="UpstreamFailed")
                            record_failure(span, diagnostic, comparison_id)
                    yield event
            except asyncio.CancelledError as exc:
                record_failure(span, failure("hosted.model.stream", exc), comparison_id)
                raise

    host.add_middleware(HostedRequestGuard)
    host.add_middleware(RequestGuard, path="/responses", body_limit=MAX_RAW_BYTES, concurrent=8, per_minute=24)
    return host


def main():
    create_app().run(host="0.0.0.0", port=8088)


if __name__ == "__main__":
    main()
