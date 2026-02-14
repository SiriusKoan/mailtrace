"""OpenTelemetry setup and trace generation functions."""

import logging
import secrets
from typing import Dict

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, SpanContext, TraceFlags

from mailtrace.tracing.delay_parser import parse_delay_info
from mailtrace.tracing.models import EmailTrace, ServiceStage

logger = logging.getLogger("mailtrace")


def setup_otel_exporter(endpoint: str) -> OTLPSpanExporter:
    """Setup OpenTelemetry OTLP exporter."""
    return OTLPSpanExporter(
        endpoint=endpoint,
        insecure=True,
    )


def create_tracer_for_host(hostname: str, exporter: OTLPSpanExporter):
    """Create a tracer instance for a specific hostname."""
    resource = Resource(
        attributes={"service.name": hostname, "service.version": "1.0.0"}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider.get_tracer(__name__), provider


def get_root_span_tracer(exporter: OTLPSpanExporter):
    """Create a tracer for the root span with service.name=mailtrace."""
    resource = Resource(
        attributes={"service.name": "mailtrace", "service.version": "1.0.0"}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider.get_tracer(__name__), provider


def message_id_to_trace_id(message_id: str) -> int:
    """Convert message ID to a valid trace ID (16 bytes / 128 bits)."""
    # Use hash to generate consistent trace ID from message ID
    return abs(hash(message_id)) % (2**128)


def generate_random_span_id() -> int:
    """Generate a random span ID (8 bytes / 64 bits)."""
    return secrets.randbits(64)


def _create_root_span(
    root_tracer: trace.Tracer,
    email_trace: EmailTrace,
    trace_id: int,
    start_ts: int,
) -> trace.Span:
    """Create and configure the root span for an email delivery trace.

    Args:
        root_tracer: The root tracer instance
        email_trace: The email trace data
        trace_id: The consistent trace ID
        start_ts: Start timestamp in nanoseconds

    Returns:
        The configured root span
    """
    # Create a SpanContext with the consistent trace ID and random span ID for linking
    span_context = SpanContext(
        trace_id=trace_id,
        span_id=generate_random_span_id(),
        is_remote=False,
        trace_flags=TraceFlags(0x01),  # sampled
    )

    # Create a link to the span context to establish the trace ID
    link = Link(span_context)

    root_span = root_tracer.start_span(
        name="email.delivery",
        kind=trace.SpanKind.INTERNAL,
        links=[link],
        start_time=start_ts,
    )

    # Set root span attributes
    root_span.set_attribute("mail.queue_ids", str(list(email_trace.queue_ids)))
    root_span.set_attribute("service.name", "mailtrace")
    if email_trace.sender:
        root_span.set_attribute("mail.from", email_trace.sender)
    if email_trace.recipient:
        root_span.set_attribute("mail.to", email_trace.recipient)
    if email_trace.message_id:
        root_span.set_attribute("mail.message_id", email_trace.message_id)

    if email_trace.end_time and email_trace.start_time:
        duration = (
            email_trace.end_time - email_trace.start_time
        ).total_seconds()
        root_span.set_attribute("mail.duration_seconds", duration)

    return root_span


def _set_root_span_host_attributes(
    root_span: trace.Span,
    stages: list[ServiceStage],
) -> None:
    """Set host-related attributes on the root span.

    Args:
        root_span: The root span to configure
        stages: List of ServiceStage objects with host information
    """
    root_span.set_attribute("mail.stages_count", len(stages))

    unique_hosts = set(stage.hostname for stage in stages)
    root_span.set_attribute("mail.hosts_count", len(unique_hosts))
    root_span.set_attribute("mail.hosts", ",".join(sorted(unique_hosts)))


def _create_stage_span(
    host_tracer: trace.Tracer,
    hostname: str,
    stage_name: str,
    trace_id: int,
    stage_start_ts: int,
    stage_end_ts: int,
) -> trace.Span:
    """Create a span for a single service stage.

    Args:
        host_tracer: The tracer for this host
        hostname: The hostname for this stage
        stage_name: The name of the service stage
        trace_id: The trace ID for this span
        stage_start_ts: Stage start timestamp in nanoseconds
        stage_end_ts: Stage end timestamp in nanoseconds

    Returns:
        The configured stage span
    """
    min_duration_ns = 1000
    if stage_end_ts <= stage_start_ts:
        stage_end_ts = stage_start_ts + min_duration_ns

    # Create a SpanContext with the consistent trace ID and random span ID for linking
    span_context = SpanContext(
        trace_id=trace_id,
        span_id=generate_random_span_id(),
        is_remote=False,
        trace_flags=TraceFlags(0x01),  # sampled
    )

    # Create a link to the span context to establish the trace ID
    link = Link(span_context)

    # Create stage span with full name including hostname
    # Parent relationship is automatically established by use_span() context
    stage_span = host_tracer.start_span(
        name=f"{hostname}/{stage_name}",
        kind=trace.SpanKind.INTERNAL,
        links=[link],
        start_time=stage_start_ts,
    )

    return stage_span


def _set_stage_span_attributes(
    stage_span: trace.Span,
    hostname: str,
    stage_name: str,
    entries: list,
) -> None:
    """Set attributes on a stage span.

    Args:
        stage_span: The stage span to configure
        hostname: The hostname for this stage
        stage_name: The name of the service stage
        entries: Log entries for this stage
    """
    # service.name should be <hostname>.<stage_name>
    service_name = f"{hostname}.{stage_name}"
    stage_span.set_attribute("service.name", service_name)

    # host.name should be just the hostname
    stage_span.set_attribute("host.name", hostname)

    # Additional mail-specific attributes
    stage_span.set_attribute("mail.hostname", hostname)
    stage_span.set_attribute("mail.service", service_name)
    stage_span.set_attribute("mail.stage_name", stage_name)

    if entries:
        entry = entries[0]
        if entry.mail_id:
            stage_span.set_attribute("mail.queue_id", entry.mail_id)

        # Add relay information
        if entry.relay_host:
            stage_span.set_attribute("mail.relay_host", entry.relay_host)
        if entry.relay_ip:
            stage_span.set_attribute("mail.relay_ip", entry.relay_ip)
        if entry.relay_port:
            stage_span.set_attribute("mail.relay_port", str(entry.relay_port))
        if entry.smtp_code:
            stage_span.set_attribute("mail.smtp_code", str(entry.smtp_code))
        if entry.queued_as:
            stage_span.set_attribute("mail.queued_as", entry.queued_as)

        # Add delay information
        delay_info = parse_delay_info(entry.message)
        if delay_info["delay"] is not None:
            stage_span.set_attribute("mail.delay", delay_info["delay"])
        if delay_info["delay_before_qmgr"] is not None:
            stage_span.set_attribute(
                "mail.delay_before_qmgr", delay_info["delay_before_qmgr"]
            )
        if delay_info["delay_in_qmgr"] is not None:
            stage_span.set_attribute(
                "mail.delay_in_qmgr", delay_info["delay_in_qmgr"]
            )
        if delay_info["delay_conn_setup"] is not None:
            stage_span.set_attribute(
                "mail.delay_conn_setup", delay_info["delay_conn_setup"]
            )
        if delay_info["delay_transmission"] is not None:
            stage_span.set_attribute(
                "mail.delay_transmission", delay_info["delay_transmission"]
            )


def _create_stage_spans(
    root_span: trace.Span,
    root_tracer: trace.Tracer,
    tracers_by_host: Dict[str, trace.Tracer],
    trace_id: int,
    stages: list[ServiceStage],
) -> None:
    """Create and configure spans for all service stages.

    Creates a hierarchical span structure:
    - root_span contains host spans
    - each host span contains four delay spans (before_qmgr, in_qmgr, conn_setup, transmission)

    Args:
        root_span: The root span to add stage spans under
        root_tracer: The root tracer for fallback
        tracers_by_host: Mapping of hostname to tracer
        trace_id: The consistent trace ID
        stages: List of service stages
    """
    min_duration_ns = 1000

    logger.debug(f"Creating {len(stages)} stage spans for email trace")

    # Group stages by hostname to create host spans
    stages_by_hostname: Dict[str, list] = {}
    for stage in stages:
        hostname = stage.hostname
        if hostname not in stages_by_hostname:
            stages_by_hostname[hostname] = []
        stages_by_hostname[hostname].append(stage)

    with trace.use_span(root_span, end_on_exit=False):
        # Create a host span for each hostname
        for hostname, host_stages in stages_by_hostname.items():
            # Get tracer for this host, fallback to root_tracer
            host_tracer = tracers_by_host.get(hostname, root_tracer)

            # Calculate host span time range from all its delay stages
            host_start_ts = int(
                host_stages[0].start_time.timestamp() * 1e9
            )  # First stage start
            host_end_ts = int(
                host_stages[-1].end_time.timestamp() * 1e9
            )  # Last stage end

            # Ensure minimum duration
            if host_end_ts <= host_start_ts:
                host_end_ts = host_start_ts + min_duration_ns

            # Create the host span
            host_span = host_tracer.start_span(
                name=f"{hostname}",
                kind=trace.SpanKind.INTERNAL,
                start_time=host_start_ts,
            )
            host_span.set_attribute("host.name", hostname)

            logger.debug(
                f"Creating host span: {hostname} with {len(host_stages)} delay stages"
            )

            # Create delay spans as children of host span
            with trace.use_span(host_span, end_on_exit=False):
                for stage in host_stages:
                    stage_start_ts = int(stage.start_time.timestamp() * 1e9)
                    stage_end_ts = int(stage.end_time.timestamp() * 1e9)

                    logger.debug(
                        f"Creating stage span: {stage.hostname}.{stage.stage_name} with {len(stage.entries)} entries"
                    )

                    # Create the stage span as child of host span
                    stage_span = _create_stage_span(
                        host_tracer,
                        stage.hostname,
                        stage.stage_name,
                        trace_id,
                        stage_start_ts,
                        stage_end_ts,
                    )

                    # Set all attributes
                    _set_stage_span_attributes(
                        stage_span,
                        stage.hostname,
                        stage.stage_name,
                        stage.entries,
                    )

                    # End the span with proper timestamp
                    # Ensure minimum 1 microsecond (1000 nanoseconds) duration
                    if stage_end_ts <= stage_start_ts:
                        stage_end_ts = stage_start_ts + min_duration_ns
                    elif stage_end_ts - stage_start_ts < min_duration_ns:
                        # If calculated duration is less than minimum, enforce it
                        stage_end_ts = stage_start_ts + min_duration_ns
                    stage_span.end(end_time=stage_end_ts)
                    logger.debug(
                        f"Stage span ended: {stage.hostname}.{stage.stage_name}"
                    )

            # End the host span
            host_span.end(end_time=host_end_ts)
            logger.debug(f"Host span ended: {hostname}")


def generate_trace_from_email(
    root_tracer: trace.Tracer,
    tracers_by_host: Dict[str, trace.Tracer],
    email_trace: EmailTrace,
) -> None:
    """Generate distributed trace spans for a single email transaction.

    This function orchestrates the creation of a complete distributed trace
    including child spans for each service stage, followed by a root span.
    The root span's duration is determined by the child spans.

    Args:
        root_tracer: The tracer for root spans
        tracers_by_host: Mapping of hostname to tracer instances
        email_trace: The email trace data to generate spans from
    """
    if not email_trace.entries:
        logger.debug(f"No entries for message ID {email_trace.message_id}")
        return

    stages = email_trace.get_service_stages()
    if not stages:
        logger.debug(
            f"No stages created for message ID {email_trace.message_id}"
        )
        return

    logger.info(
        f"Generating trace for queue ID {email_trace.message_id} with {len(stages)} stages"
    )

    # Generate consistent trace ID from message ID
    trace_id = message_id_to_trace_id(email_trace.message_id)

    # Create a temporary root span to use as context for child spans
    # We'll end it with 0 duration initially, then update with actual duration
    min_duration_ns = 1000
    first_stage_start = stages[0].start_time
    start_ts = int(first_stage_start.timestamp() * 1e9)

    # Create root span with 0 initial duration (will be updated after child spans)
    temp_root_span = _create_root_span(
        root_tracer,
        email_trace,
        trace_id,
        start_ts,
    )

    # Set host-related attributes on root span
    _set_root_span_host_attributes(temp_root_span, stages)

    # Create spans for each stage (will be children of root span via use_span context)
    _create_stage_spans(
        temp_root_span,
        root_tracer,
        tracers_by_host,
        trace_id,
        stages,
    )

    # Now calculate actual root span duration from last child span
    last_stage_end = stages[-1].end_time
    end_ts = int(last_stage_end.timestamp() * 1e9)

    # Ensure minimum 1 microsecond duration for root span
    if end_ts <= start_ts:
        end_ts = start_ts + min_duration_ns

    # End the root span with the correct duration
    temp_root_span.end(end_time=end_ts)
