"""OpenTelemetry module for email trace span generation.

This module provides OpenTelemetry integration for generating distributed
traces from email delivery logs.

Submodules:
- exporter: OTLP exporter setup
- tracer: Tracer provider creation
- context: Span context utilities and helpers
- builders: Span builder classes for creating spans
- generator: Trace generation orchestration
"""

from mailtrace.tracing.otel.context import (
    SpanContextFactory,
    generate_random_span_id,
    message_id_to_trace_id,
)
from mailtrace.tracing.otel.exporter import setup_otel_exporter
from mailtrace.tracing.otel.generator import (
    TraceGenerator,
    generate_trace_from_email,
)
from mailtrace.tracing.otel.span_builders import (
    DelaySpanBuilder,
    HostSpanBuilder,
    RootSpanBuilder,
)
from mailtrace.tracing.otel.tracer import (
    create_tracer_for_host,
    get_root_span_tracer,
)

__all__ = [
    "setup_otel_exporter",
    "create_tracer_for_host",
    "get_root_span_tracer",
    "message_id_to_trace_id",
    "generate_random_span_id",
    "SpanContextFactory",
    "RootSpanBuilder",
    "HostSpanBuilder",
    "DelaySpanBuilder",
    "TraceGenerator",
    "generate_trace_from_email",
]
