"""OpenTelemetry setup: traces and metrics for every tool call and LLM call.

Exporting is opt-in:
- OTEL_EXPORTER_OTLP_ENDPOINT set  -> OTLP/HTTP export (Jaeger, Grafana Tempo, Honeycomb, ...)
- SAFESQL_OTEL_CONSOLE=1           -> spans printed to stderr (stdout is the MCP stdio channel)
- neither                          -> spans are created but not exported
"""

from __future__ import annotations

import os
import sys
import threading

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

from . import __version__

_lock = threading.Lock()
_configured = False


def setup_telemetry(service_name: str = "safesql-mcp") -> None:
    """Install tracer and meter providers once per process. Safe to call repeatedly."""
    global _configured
    with _lock:
        if _configured:
            return
        _configured = True
        resource = Resource.create({"service.name": service_name, "service.version": __version__})
        tracer_provider = TracerProvider(resource=resource)
        readers = []
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=10_000))
        if os.environ.get("SAFESQL_OTEL_CONSOLE") == "1":
            tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))


tracer = trace.get_tracer("safesql", __version__)
_meter = metrics.get_meter("safesql", __version__)

query_duration = _meter.create_histogram(
    "safesql.query.duration", unit="ms", description="End-to-end run_query latency"
)
query_decisions = _meter.create_counter(
    "safesql.query.decisions", description="run_query outcomes by decision and reason code"
)
llm_tokens = _meter.create_counter(
    "gen_ai.client.token.usage", unit="{token}", description="LLM tokens by type"
)
llm_cost = _meter.create_counter("safesql.llm.cost", unit="USD", description="Estimated LLM spend")
