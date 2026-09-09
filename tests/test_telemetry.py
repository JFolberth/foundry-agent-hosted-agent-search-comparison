import json
from unittest.mock import patch

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from comparison import telemetry
from comparison.evidence import extract_evidence


def test_tool_evidence_spans_have_correlation_not_content(raw_response, monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(telemetry, "tracer", tracer)
    with tracer.start_as_current_span("model") as span:
        telemetry.record_evidence(span, extract_evidence(raw_response), "correlation-1")
    spans = exporter.get_finished_spans()
    native = next(span for span in spans if span.name == "native_tool.evidence")
    assert native.attributes["comparison.id"] == "correlation-1"
    assert native.attributes["comparison.evidence_only"] is True
    assert native.attributes["gen_ai.tool.id"] == "search_1"
    serialized = json.dumps([dict(span.attributes) for span in spans])
    assert "private-ciphertext" not in serialized
    assert "Indexed source." not in serialized
    assert "What is indexed?" not in serialized
    provider.shutdown()


def test_azure_monitor_explicitly_disables_content_instrumentation(monkeypatch):
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=test")
    with patch("azure.monitor.opentelemetry.configure_azure_monitor") as configure:
        telemetry.configure_telemetry("test-service")
    options = configure.call_args.kwargs
    assert options["logger_name"] == "comparison.safe"
    assert options["disable_offline_storage"] is True
    assert options["sampling_ratio"] == 1.0
    assert options["instrumentation_options"]["openai"]["enabled"] is False
    assert options["instrumentation_options"]["azure_sdk"]["enabled"] is False
