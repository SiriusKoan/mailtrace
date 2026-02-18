"""OpenTelemetry trace generation from email trace data."""

import logging
from typing import Dict

from opentelemetry import trace

from mailtrace.parser import LogEntry
from mailtrace.tracing.delay_builder import DelayBuilder
from mailtrace.tracing.models import Delay, EmailTrace
from mailtrace.tracing.otel.context import (
    SpanContextFactory,
    message_id_to_trace_id,
)
from mailtrace.tracing.otel.span_builders import (
    DelaySpanBuilder,
    HostSpanBuilder,
    RootSpanBuilder,
)

logger = logging.getLogger("mailtrace")

# Minimum span duration in nanoseconds (1 microsecond)
MIN_SPAN_DURATION_NS = 1000


class TraceGenerator:
    """Generates distributed traces from email trace data."""

    def __init__(
        self,
        root_tracer: trace.Tracer,
        tracers_by_host: Dict[str, trace.Tracer],
    ):
        """Initialize the generator.

        Args:
            root_tracer: The tracer for root spans
            tracers_by_host: Mapping of hostname to tracer instances
        """
        self.root_tracer = root_tracer
        self.tracers_by_host = tracers_by_host
        self.delay_builder = DelayBuilder()

    def generate(self, email_trace: EmailTrace) -> None:
        """Generate distributed trace spans for an email transaction.

        Creates a hierarchical span structure:
        - Root span (email.delivery)
          - Host span (hostname)
            - Delay spans (before_qmgr, in_qmgr, conn_setup, transmission)

        Args:
            email_trace: The email trace data to generate spans from
        """
        if not email_trace.entries:
            logger.debug(f"No entries for message ID {email_trace.message_id}")
            return

        delays = self.delay_builder.build_delays(
            email_trace.message_id, email_trace.entries
        )

        if not delays:
            logger.debug(
                f"No delays created for message ID {email_trace.message_id}"
            )
            return

        logger.info(
            f"Generating trace for message ID {email_trace.message_id} "
            f"with {len(delays)} delays"
        )

        trace_id = message_id_to_trace_id(email_trace.message_id)
        context_factory = SpanContextFactory(trace_id)

        # Create root span (timing will be adjusted after child spans)
        root_span_builder = RootSpanBuilder(self.root_tracer, context_factory)
        root_span = root_span_builder.build(email_trace, delays)

        # Group entries by hostname for attribute setting
        hostname_entries = self._group_entries_by_hostname(email_trace.entries)

        # Create host and delay spans
        with trace.use_span(root_span, end_on_exit=False):
            self._create_host_and_delay_spans(
                delays, context_factory, hostname_entries
            )

        # End root span at the last delay's end time
        root_end_ts = int(delays[-1].end_time.timestamp() * 1e9)
        root_start_ts = int(delays[0].start_time.timestamp() * 1e9)

        # Ensure minimum duration
        if root_end_ts <= root_start_ts:
            root_end_ts = root_start_ts + MIN_SPAN_DURATION_NS

        root_span.end(end_time=root_end_ts)

    def _group_entries_by_hostname(
        self, entries: list[LogEntry]
    ) -> dict[str, list[LogEntry]]:
        """Group log entries by hostname.

        Args:
            entries: List of log entries

        Returns:
            Mapping of hostname to list of entries
        """
        hostname_entries: dict[str, list[LogEntry]] = {}
        for entry in entries:
            if entry.hostname not in hostname_entries:
                hostname_entries[entry.hostname] = []
            hostname_entries[entry.hostname].append(entry)
        return hostname_entries

    def _create_host_and_delay_spans(
        self,
        delays: list[Delay],
        context_factory: SpanContextFactory,
        hostname_entries: dict[str, list[LogEntry]],
    ) -> None:
        """Create host spans with nested delay spans.

        Args:
            delays: List of Delay objects
            context_factory: Factory for creating span contexts
            hostname_entries: Mapping of hostname to log entries
        """
        # Group delays by hostname
        delays_by_host: dict[str, list[Delay]] = {}
        for delay in delays:
            if delay.hostname not in delays_by_host:
                delays_by_host[delay.hostname] = []
            delays_by_host[delay.hostname].append(delay)

        # Create a host span for each hostname
        for hostname, host_delays in delays_by_host.items():
            host_tracer = self.tracers_by_host.get(hostname, self.root_tracer)

            # Get MTA type for this specific host
            host_mta_type = self.delay_builder.detected_mta_by_host.get(
                hostname
            )

            logger.debug(
                f"Creating host span: {hostname} with {len(host_delays)} delays"
            )

            # Create host span (timing based on delays)
            host_span_builder = HostSpanBuilder(host_tracer, host_mta_type)
            host_span = host_span_builder.build(hostname, host_delays)

            # Create delay spans under host span
            with trace.use_span(host_span, end_on_exit=False):
                delay_span_builder = DelaySpanBuilder(
                    host_tracer, context_factory, hostname_entries
                )

                for delay in host_delays:
                    logger.debug(
                        f"Creating delay span: {delay.hostname}/{delay.name}"
                    )
                    delay_span, end_ts = delay_span_builder.build(delay)
                    delay_span.end(end_time=end_ts)
                    logger.debug(
                        f"Delay span ended: {delay.hostname}/{delay.name}"
                    )

            # End host span at the last delay's end time
            host_end_ts = int(host_delays[-1].end_time.timestamp() * 1e9)
            host_start_ts = int(host_delays[0].start_time.timestamp() * 1e9)

            # Ensure minimum duration
            if host_end_ts <= host_start_ts:
                host_end_ts = host_start_ts + MIN_SPAN_DURATION_NS

            host_span.end(end_time=host_end_ts)
            logger.debug(f"Host span ended: {hostname}")


def generate_trace_from_email(
    root_tracer: trace.Tracer,
    tracers_by_host: Dict[str, trace.Tracer],
    email_trace: EmailTrace,
) -> None:
    """Generate distributed trace spans for a single email transaction.

    This is the main entry point for trace generation. It creates a
    complete distributed trace with root span, host spans, and delay spans.

    Args:
        root_tracer: The tracer for root spans
        tracers_by_host: Mapping of hostname to tracer instances
        email_trace: The email trace data to generate spans from
    """
    generator = TraceGenerator(root_tracer, tracers_by_host)
    generator.generate(email_trace)
