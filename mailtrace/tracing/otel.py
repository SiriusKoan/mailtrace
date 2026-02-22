from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from mailtrace.tracing.delay_parser import DelayInfo

_exporter: Optional[OTLPSpanExporter] = None
_providers: dict[str, TracerProvider] = {}

logger = logging.getLogger("mailtrace")


def init_exporter(endpoint: str) -> None:
    """Initialise the shared OTLP exporter.

    Must be called once (e.g. at application startup) before any
    ``create_*`` function is used.  Clears all cached
    :class:`~opentelemetry.sdk.trace.TracerProvider` instances so a fresh
    exporter connection is used.

    Args:
        endpoint: OTLP gRPC endpoint, e.g. ``"http://localhost:4317"``.
    """
    global _exporter
    _exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    _providers.clear()


def flush_traces() -> None:
    """Force-flush every cached provider.

    Blocks until all buffered spans have been delivered to the collector.
    Call this once after all spans for a polling cycle have been ended.
    """
    for provider in _providers.values():
        provider.force_flush()


def _get_tracer(service_name: str) -> trace.Tracer:
    """Return (and lazily create) a tracer for *service_name*."""
    if service_name not in _providers:
        resource = Resource(
            attributes={
                "service.name": service_name,
                "service.version": "1.0.0",
            }
        )
        provider = TracerProvider(resource=resource)
        if _exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(_exporter))
        _providers[service_name] = provider
    return _providers[service_name].get_tracer(__name__)


def dt_to_ns(dt: datetime) -> int:
    """Convert a :class:`~datetime.datetime` to an integer nanosecond timestamp."""
    return int(dt.timestamp() * 1e9)


def create_root_span(message_id: str, start_time: datetime) -> trace.Span:
    """Create and start the root span for one email delivery trace.

    The span is started but **not** ended — the caller must call
    ``span.end(end_time=...)`` once all child spans have been ended.

    Args:
        message_id: The RFC 2822 ``Message-ID`` header value.
        start_time: Absolute start time for the span.

    Returns:
        A live (not-yet-ended) SDK :class:`~opentelemetry.sdk.trace.Span`.
    """
    tracer = _get_tracer("mailtrace")
    return tracer.start_span(
        name="email.delivery",
        start_time=dt_to_ns(start_time),
        attributes={"message.id": message_id},
    )


def create_host_span(
    hostname: str,
    start_time: datetime,
    parent_context: Any,
) -> trace.Span:
    """Create and start a host span as a child of *parent_context*.

    The span is started but **not** ended — the caller must call
    ``span.end(end_time=...)`` once all child spans have been ended.

    A dedicated :class:`~opentelemetry.sdk.trace.TracerProvider` with
    ``service.name=hostname`` is used so the host appears as a separate
    service in the trace back-end (e.g. Jaeger, Grafana Tempo).

    Args:
        hostname: The mail-server hostname.
        start_time: Absolute start time for the span.
        parent_context: OTEL :class:`~opentelemetry.context.Context` that
            carries the parent span (typically obtained via
            ``trace.set_span_in_context(parent_span)``).

    Returns:
        A live (not-yet-ended) SDK :class:`~opentelemetry.sdk.trace.Span`.
    """
    tracer = _get_tracer(hostname)
    return tracer.start_span(
        name=hostname,
        context=parent_context,
        start_time=dt_to_ns(start_time),
        attributes={"server.address": hostname},
    )


def create_delay_spans(
    delays: DelayInfo,
    hostname: str,
    start_time: datetime,
    parent_context: Any,
) -> list[trace.Span]:
    """Create, start, and end one span per delay stage.

    All stage spans share the same *parent_context* (the host span) so they
    appear as siblings under the host in the trace view.  Each span is
    started with the correct sequential start time derived from *start_time*
    and the cumulative durations, and immediately ended with the appropriate
    end time based on the stage duration.

    Args:
        delays: A :class:`~mailtrace.tracing.delay_parser.DelayInfo` object
            containing the delay stages and their durations in seconds.
        hostname: The mail-server hostname; used to look up the correct
            tracer so stage spans share the host's ``service.name``.
        start_time: Absolute start time of the *first* stage.
        parent_context: OTEL :class:`~opentelemetry.context.Context` that
            carries the parent (host) span.

    Returns:
        List of completed SDK :class:`~opentelemetry.sdk.trace.Span`
        objects, one per stage, in the same order as the stages in *delays*.
    """

    tracer = _get_tracer(hostname)
    spans: list[trace.Span] = []
    current = start_time
    stage_names = delays.get_delay_values().keys()
    for name, duration in zip(stage_names, delays.get_delay_values().values()):
        span = tracer.start_span(
            name=name,
            context=parent_context,
            start_time=dt_to_ns(current),
            attributes={"delay.duration_seconds": duration},
        )
        span.end(end_time=dt_to_ns(current + timedelta(seconds=duration)))
        logger.debug(
            f"Created span for stage {name} (start={current}, end={current + timedelta(seconds=duration)})"
        )
        spans.append(span)
        current = current + timedelta(seconds=duration)
    return spans
