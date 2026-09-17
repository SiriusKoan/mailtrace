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
MIN_SPAN_DURATION_SECONDS = 2e-6

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


def _get_tracer(
    service_name: str, host_name: Optional[str] = None
) -> trace.Tracer:
    """Return or create a tracer with service and optional host identity."""
    if service_name not in _providers:
        attributes = {
            "service.name": service_name,
            "service.version": "1.0.0",
        }
        if host_name is not None:
            attributes["host.name"] = host_name
        resource = Resource(attributes=attributes)
        provider = TracerProvider(resource=resource)
        if _exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(_exporter))
        _providers[service_name] = provider
    return _providers[service_name].get_tracer(__name__)


def dt_to_ns(dt: datetime) -> int:
    """Convert a :class:`~datetime.datetime` to an integer nanosecond timestamp."""
    return int(dt.timestamp() * 1e9)


def _get_effective_delay_duration(duration: float) -> float:
    return max(duration, MIN_SPAN_DURATION_SECONDS)


def get_effective_total_delay(delays: DelayInfo) -> float:
    """Return the total delay after applying the minimum span duration."""
    return sum(
        _get_effective_delay_duration(duration)
        for duration in delays.get_delay_values().values()
    )


def mark_span_failed(
    span: trace.Span,
    error_type: str,
    mail_status: Optional[str] = None,
    smtp_response_code: Optional[int] = None,
    smtp_enhanced_status_code: Optional[str] = None,
) -> None:
    span.set_status(trace.Status(trace.StatusCode.ERROR))
    span.set_attribute("error.type", error_type)
    if mail_status is not None:
        span.set_attribute("mail.delivery_status", mail_status)
    if smtp_response_code is not None:
        span.set_attribute("smtp.response_code", smtp_response_code)
    if smtp_enhanced_status_code is not None:
        span.set_attribute(
            "smtp.enhanced_status_code", smtp_enhanced_status_code
        )


def create_root_span(
    message_id: str,
    start_time: datetime,
    sender: Optional[str] = None,
    recipients: Optional[list[str]] = None,
) -> trace.Span:
    """Create and start the root span for one email delivery trace.

    The span is started but **not** ended — the caller must call
    ``span.end(end_time=...)`` once all child spans have been ended.

    Args:
        message_id: The RFC 2822 ``Message-ID`` header value.
        start_time: Absolute start time for the span.
        sender: The email sender address (optional).
        recipients: List of email recipient addresses (optional).

    Returns:
        A live (not-yet-ended) SDK :class:`~opentelemetry.sdk.trace.Span`.
    """
    tracer = _get_tracer("mailtrace")
    attributes = {"message.id": message_id}
    if sender is not None:
        attributes["email.sender"] = sender
    if recipients is not None:
        attributes["email.recipients"] = ",".join(recipients)
    return tracer.start_span(
        name="email.delivery",
        start_time=dt_to_ns(start_time),
        attributes=attributes,
    )


def create_host_span(
    hostname: str,
    start_time: datetime,
    parent_context: Any,
    message_id: Optional[str] = None,
    sender: Optional[str] = None,
    recipients: Optional[list[str]] = None,
    queue_id: Optional[str] = None,
    next_host: Optional[str] = None,
    explicit_handoff: Optional[bool] = None,
    next_queue_id: Optional[str] = None,
    relay_host: Optional[str] = None,
    relay_ip: Optional[str] = None,
    relay_port: Optional[int] = None,
    smtp_response_code: Optional[int] = None,
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
        message_id: The RFC 2822 ``Message-ID`` header value (optional).
        sender: The email sender address (optional).
        recipients: List of email recipient addresses (optional).
        queue_id: Queue ID for this host (optional).
        next_host: Next host this host relays the message to (optional).
        explicit_handoff: Whether the incoming queue handoff is explicit
            (optional).
        next_queue_id: Queue ID created by the next hop (optional).
        relay_host: Relay hostname (optional).
        relay_ip: Relay IP address (optional).
        relay_port: Relay port (optional).
        smtp_response_code: SMTP response code (optional).

    Returns:
        A live (not-yet-ended) SDK :class:`~opentelemetry.sdk.trace.Span`.
    """
    tracer = _get_tracer(hostname, host_name=hostname)
    attributes: dict[str, Any] = {"server.address": hostname}
    if message_id is not None:
        attributes["message.id"] = message_id
    if sender is not None:
        attributes["email.sender"] = sender
    if recipients is not None:
        attributes["email.recipients"] = ",".join(recipients)
    if queue_id is not None:
        attributes["email.queue_id"] = queue_id
    if next_host is not None:
        attributes["email.next_host"] = next_host
    if explicit_handoff is not None:
        attributes["email.explicit_handoff"] = explicit_handoff
    if next_queue_id is not None:
        attributes["email.next_queue_id"] = next_queue_id
    if relay_host is not None:
        attributes["email.relay_host"] = relay_host
    if relay_ip is not None:
        attributes["email.relay_ip"] = relay_ip
    if relay_port is not None:
        attributes["email.relay_port"] = relay_port
    if smtp_response_code is not None:
        attributes["smtp.response_code"] = smtp_response_code
    return tracer.start_span(
        name=hostname,
        context=parent_context,
        start_time=dt_to_ns(start_time),
        attributes=attributes,
    )


def create_delivery_span(
    hostname: str,
    start_time: datetime,
    parent_context: Any,
    recipient: Optional[str] = None,
    transport: Optional[str] = None,
) -> trace.Span:
    """Create a delivery wrapper that groups one recipient's delay stages."""
    tracer = _get_tracer(hostname, host_name=hostname)
    attributes = {}
    if recipient is not None:
        attributes["email.recipient"] = recipient
    if transport is not None:
        attributes["email.transport"] = transport
    return tracer.start_span(
        name="delivery",
        context=parent_context,
        start_time=dt_to_ns(start_time),
        attributes=attributes,
    )


def create_delay_spans(
    delays: DelayInfo,
    hostname: str,
    start_time: datetime,
    parent_context: Any,
) -> list[trace.Span]:
    """Create, start, and end one span per delay stage.

    All stage spans share the same *parent_context* (one delivery wrapper) so
    they appear as siblings within a recipient-specific delivery branch. Each
    span starts at the sequential time derived from *start_time* and the
    cumulative durations, then ends at the corresponding stage boundary.

    Args:
        delays: A :class:`~mailtrace.tracing.delay_parser.DelayInfo` object
            containing the delay stages and their durations in seconds.
        hostname: The mail-server hostname; used to look up the correct
            tracer so stage spans share the host's ``service.name``.
        start_time: Absolute start time of the *first* stage.
        parent_context: OTEL :class:`~opentelemetry.context.Context` that
            carries the parent delivery span.

    Returns:
        List of completed SDK :class:`~opentelemetry.sdk.trace.Span`
        objects, one per stage, in the same order as the stages in *delays*.
    """

    tracer = _get_tracer(hostname, host_name=hostname)
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

        # Avoid zero-duration spans that tracing backends may ignore.
        duration = _get_effective_delay_duration(duration)
        span.end(end_time=dt_to_ns(current + timedelta(seconds=duration)))

        logger.debug(
            f"Created span for stage {name} (start={current}, end={current + timedelta(seconds=duration)})"
        )
        spans.append(span)
        current = current + timedelta(seconds=duration)
    return spans
