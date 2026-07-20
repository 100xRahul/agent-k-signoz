"""Full OpenTelemetry setup for Agent K — traces, metrics, logs + gen_ai semconv."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.metrics import Counter, Histogram
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.trace import Span, StatusCode

logger = logging.getLogger(__name__)

# ── Globals (initialized in setup_telemetry) ─────────────────────

_tracer: trace.Tracer | None = None
_investigations_counter: Counter | None = None
_investigation_duration: Histogram | None = None
_cost_counter: Counter | None = None


def get_tracer() -> trace.Tracer:
    """Get the Agent K tracer."""
    if _tracer is None:
        return trace.get_tracer("agent-k")
    return _tracer


def setup_telemetry(app: Any | None = None) -> None:
    """Initialize OTel providers, exporters, and instrument FastAPI."""
    global _tracer, _investigations_counter, _investigation_duration, _cost_counter

    resource = Resource.create(
        {
            "service.name": "agent-k",
            "service.version": "1.0.0",
            "deployment.environment": "production",
        }
    )

    # ── Traces ────────────────────────────────────────────────────
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter()
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)
    _tracer = tracer_provider.get_tracer("agent-k", "1.0.0")

    # ── Metrics ───────────────────────────────────────────────────
    metric_exporter = OTLPMetricExporter()
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter, export_interval_millis=15000
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    meter = meter_provider.get_meter("agent-k", "1.0.0")
    _investigations_counter = meter.create_counter(
        "agentk.investigations",
        description="Total investigations started",
        unit="1",
    )
    _investigation_duration = meter.create_histogram(
        "agentk.investigation.duration",
        description="Investigation duration in seconds",
        unit="s",
    )
    _cost_counter = meter.create_counter(
        "agentk.cost.usd",
        description="Cumulative LLM cost in USD",
        unit="USD",
    )

    # ── Logs ──────────────────────────────────────────────────────
    log_exporter = OTLPLogExporter()
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    # Root logger defaults to WARNING, which would swallow every logger.info
    # in the agent — both on the console and in the OTLP export.
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().addHandler(handler)

    # ── FastAPI instrumentation ───────────────────────────────────
    if app is not None:
        FastAPIInstrumentor.instrument_app(app)

    logger.info("Agent K telemetry initialized")


# ── Metric helpers ────────────────────────────────────────────────


def record_investigation_started(alertname: str = "") -> None:
    """Increment the investigations counter."""
    if _investigations_counter:
        _investigations_counter.add(1, {"agentk.alertname": alertname})


def record_investigation_duration(duration_s: float, alertname: str = "") -> None:
    """Record investigation duration."""
    if _investigation_duration:
        _investigation_duration.record(duration_s, {"agentk.alertname": alertname})


def record_cost(cost_usd: float, model: str = "") -> None:
    """Record LLM cost."""
    if _cost_counter and cost_usd > 0:
        _cost_counter.add(cost_usd, {"gen_ai.request.model": model})


# ── Span helpers ──────────────────────────────────────────────────


@contextmanager
def investigation_span(
    trigger_type: str,
    alertname: str,
) -> Generator[Span, None, None]:
    """Create the root investigation span with Agent K attributes."""
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "investigation",
        attributes={
            "agentk.trigger_type": trigger_type,
            "agentk.alertname": alertname,
        },
    ) as span:
        yield span


def set_investigation_result(
    span: Span,
    root_cause: str,
    confidence: str,
    total_cost_usd: float,
) -> None:
    """Set final result attributes on the investigation span."""
    span.set_attribute("agentk.root_cause", root_cause)
    span.set_attribute("agentk.confidence", confidence)
    span.set_attribute("agentk.total_cost_usd", total_cost_usd)


@contextmanager
def llm_call_span(
    model: str,
    system: str = "openai",
) -> Generator[Span, None, None]:
    """Create a child span for an LLM call with gen_ai semconv."""
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "llm.call",
        attributes={
            "gen_ai.system": system,
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model,
        },
    ) as span:
        yield span


def set_llm_usage(
    span: Span,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    """Set usage attributes on an LLM call span (gen_ai semconv + total, which
    SigNoz's LLM examples/dashboards key on)."""
    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
    span.set_attribute("gen_ai.usage.total_tokens", input_tokens + output_tokens)
    span.set_attribute("llm.cost.usd", cost_usd)


@contextmanager
def tool_call_span(name: str, args_summary: str = "") -> Generator[Span, None, None]:
    """Create a child span for a tool call."""
    tracer = get_tracer()
    with tracer.start_as_current_span(
        f"tool.{name}",
        attributes={
            "tool.name": name,
            "tool.args_summary": args_summary[:500],
        },
    ) as span:
        yield span


def set_tool_result(span: Span, result_bytes: int, error: bool = False) -> None:
    """Set result attributes on a tool call span."""
    span.set_attribute("tool.result_bytes", result_bytes)
    span.set_attribute("error", error)
    if error:
        span.set_status(StatusCode.ERROR)


def get_current_trace_id() -> str:
    """Get the current trace ID as hex string."""
    current_span = trace.get_current_span()
    ctx = current_span.get_span_context()
    if ctx and ctx.trace_id:
        return format(ctx.trace_id, "032x")
    return ""
