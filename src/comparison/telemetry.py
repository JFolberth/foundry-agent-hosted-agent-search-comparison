import os
import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource

tracer = trace.get_tracer("comparison")


def configure_telemetry(service: str):
    # No content instrumentation or SDK console logging, even if inherited from the host.
    os.environ["AZURE_AI_PROJECTS_CONSOLE_LOGGING"] = "false"
    os.environ["AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED"] = "false"
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "false"
    for name in ("openai", "httpx", "httpcore", "azure"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if not os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"],
        logger_name="comparison.safe",
        resource=Resource.create({"service.name": service}),
        disable_offline_storage=True,
        enable_live_metrics=False,
        enable_performance_counters=False,
        sampling_ratio=1.0,
        instrumentation_options={
            name: {"enabled": False} for name in (
                "azure_sdk", "django", "fastapi", "flask", "psycopg2",
                "requests", "urllib", "urllib3", "httpx", "openai", "openai_v2",
            )
        },
    )


def record_evidence(span, result: dict, comparison_id: str):
    span.set_attribute("comparison.tool_evidence_available", result["tool_evidence_available"])
    if result.get("response_id"):
        span.set_attribute("gen_ai.response.id", result["response_id"])
    for key, value in (result.get("usage") or {}).items():
        if isinstance(value, int):
            span.set_attribute(f"gen_ai.usage.{key}", value)
    if result["tool_evidence_available"]:
        span.set_attribute("comparison.observed_tool_items", len(result["tool_calls"]))
    for item in result["tool_calls"]:
        if not isinstance(item, dict):
            continue
        with tracer.start_as_current_span(
            "native_tool.evidence", record_exception=False, set_status_on_exception=False,
        ) as tool_span:
            tool_span.set_attribute("comparison.id", comparison_id)
            tool_span.set_attribute("comparison.evidence_only", True)
            tool_span.set_attribute("gen_ai.tool.type", str(item.get("type", ""))[:100])
            for key in ("id", "call_id", "status"):
                if isinstance(item.get(key), str):
                    tool_span.set_attribute(f"gen_ai.tool.{key}", item[key][:180])


def trace_id() -> str | None:
    return telemetry_ids()["trace_id"]


def telemetry_ids() -> dict:
    span = trace.get_current_span()
    context = span.get_span_context()
    recorded = span.is_recording() and context.is_valid and context.trace_flags.sampled
    return {
        "trace_id": format(context.trace_id, "032x") if recorded else None,
        "span_id": format(context.span_id, "016x") if recorded else None,
        "telemetry_note": (
            "IDs identify recorded OpenTelemetry spans; Application Insights ingestion may be delayed."
            if recorded else "No recorded telemetry span is available for this invocation."
        ),
    }
