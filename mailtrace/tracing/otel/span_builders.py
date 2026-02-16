"""OpenTelemetry span builders for creating spans with attributes."""

import logging

from opentelemetry import trace

from mailtrace.parser import LogEntry
from mailtrace.tracing.delay_parser import parse_delay_info
from mailtrace.tracing.models import Delay, EmailTrace
from mailtrace.tracing.otel.context import SpanContextFactory

logger = logging.getLogger("mailtrace")

# Minimum span duration in nanoseconds (1 microsecond)
MIN_SPAN_DURATION_NS = 1000


class RootSpanBuilder:
    """Builder for creating root spans in email delivery traces."""

    def __init__(
        self, tracer: trace.Tracer, context_factory: SpanContextFactory
    ):
        """Initialize the builder.

        Args:
            tracer: The tracer to use for creating spans
            context_factory: Factory for creating span contexts
        """
        self.tracer = tracer
        self.context_factory = context_factory

    def build(
        self,
        email_trace: EmailTrace,
        delays: list[Delay],
    ) -> trace.Span:
        """Build and configure a root span for an email delivery trace.

        The span is created without end time - caller must call end() after
        creating all child spans to set proper timing.

        Args:
            email_trace: The email trace data
            delays: List of delay objects

        Returns:
            The configured root span (not yet ended)
        """
        # Start the root span at the first delay's start time
        start_time = int(delays[0].start_time.timestamp() * 1e9)

        root_span = self.tracer.start_span(
            name="email.delivery",
            kind=trace.SpanKind.INTERNAL,
            links=[self.context_factory.create_link()],
            start_time=start_time,
        )

        self._set_basic_attributes(root_span, email_trace)
        self._set_delay_attributes(root_span, delays)

        return root_span

    def _set_basic_attributes(
        self, span: trace.Span, email_trace: EmailTrace
    ) -> None:
        """Set basic attributes on the root span.

        Args:
            span: The span to configure
            email_trace: The email trace data
        """
        span.set_attribute("mail.queue_ids", str(list(email_trace.queue_ids)))
        span.set_attribute("service.name", "mailtrace")

        if email_trace.sender:
            span.set_attribute("mail.from", email_trace.sender)
        if email_trace.recipient:
            span.set_attribute("mail.to", email_trace.recipient)
        if email_trace.message_id:
            span.set_attribute("mail.message_id", email_trace.message_id)

        if email_trace.end_time and email_trace.start_time:
            duration = (
                email_trace.end_time - email_trace.start_time
            ).total_seconds()
            span.set_attribute("mail.duration_seconds", duration)

    def _set_delay_attributes(
        self, span: trace.Span, delays: list[Delay]
    ) -> None:
        """Set delay-related attributes on the root span.

        Args:
            span: The span to configure
            delays: List of delay objects
        """
        span.set_attribute("mail.delays_count", len(delays))

        unique_hosts = set(delay.hostname for delay in delays)
        span.set_attribute("mail.hosts_count", len(unique_hosts))
        span.set_attribute("mail.hosts", ",".join(sorted(unique_hosts)))


class HostSpanBuilder:
    """Builder for creating host-level spans."""

    def __init__(self, tracer: trace.Tracer):
        """Initialize the builder.

        Args:
            tracer: The tracer to use for creating spans
        """
        self.tracer = tracer

    def build(self, hostname: str, delays: list[Delay]) -> trace.Span:
        """Build a host span.

        The span is created with timing based on the delays - it starts at the
        first delay's start time. Caller must call end() after creating all
        child delay spans.

        Args:
            hostname: The hostname
            delays: List of delay objects for this host

        Returns:
            The configured host span (not yet ended)
        """
        # Start at first delay's start time
        start_time = int(delays[0].start_time.timestamp() * 1e9)

        span = self.tracer.start_span(
            name=hostname,
            kind=trace.SpanKind.INTERNAL,
            start_time=start_time,
        )
        span.set_attribute("host.name", hostname)

        return span


class DelaySpanBuilder:
    """Builder for creating delay spans."""

    def __init__(
        self,
        tracer: trace.Tracer,
        context_factory: SpanContextFactory,
        hostname_entries: dict[str, list[LogEntry]],
    ):
        """Initialize the builder.

        Args:
            tracer: The tracer to use for creating spans
            context_factory: Factory for creating span contexts
            hostname_entries: Mapping of hostname to log entries
        """
        self.tracer = tracer
        self.context_factory = context_factory
        self.hostname_entries = hostname_entries

    def build(self, delay: Delay) -> tuple[trace.Span, int]:
        """Build a delay span from a Delay object.

        Args:
            delay: The Delay object to create a span for

        Returns:
            Tuple of (configured delay span, end timestamp in nanoseconds)
        """
        start_ts = int(delay.start_time.timestamp() * 1e9)
        end_ts = int(delay.end_time.timestamp() * 1e9)

        # Ensure minimum duration
        if end_ts <= start_ts:
            end_ts = start_ts + MIN_SPAN_DURATION_NS

        span = self.tracer.start_span(
            name=f"{delay.hostname}/{delay.name}",
            kind=trace.SpanKind.INTERNAL,
            links=[self.context_factory.create_link()],
            start_time=start_ts,
        )

        self._set_attributes(span, delay)

        return span, end_ts

    def _set_attributes(self, span: trace.Span, delay: Delay) -> None:
        """Set attributes on a delay span.

        Args:
            span: The span to configure
            delay: The Delay object with information
        """
        service_name = f"{delay.hostname}.{delay.name}"
        span.set_attribute("service.name", service_name)
        span.set_attribute("host.name", delay.hostname)
        span.set_attribute("mail.hostname", delay.hostname)
        span.set_attribute("mail.service", service_name)
        span.set_attribute("mail.delay_name", delay.name)

        # Get entries for this hostname
        entries = self.hostname_entries.get(delay.hostname, [])
        if entries:
            self._set_log_entry_attributes(span, entries[0])

    def _set_log_entry_attributes(
        self, span: trace.Span, entry: LogEntry
    ) -> None:
        """Set attributes from a log entry.

        Args:
            span: The span to configure
            entry: The log entry to extract attributes from
        """
        if entry.mail_id:
            span.set_attribute("mail.queue_id", entry.mail_id)

        # Add relay information
        if entry.relay_host:
            span.set_attribute("mail.relay_host", entry.relay_host)
        if entry.relay_ip:
            span.set_attribute("mail.relay_ip", entry.relay_ip)
        if entry.relay_port:
            span.set_attribute("mail.relay_port", str(entry.relay_port))
        if entry.smtp_code:
            span.set_attribute("mail.smtp_code", str(entry.smtp_code))
        if entry.queued_as:
            span.set_attribute("mail.queued_as", entry.queued_as)

        # Add delay information
        delay_info = parse_delay_info(entry.message)
        if delay_info.total_delay is not None:
            span.set_attribute("mail.delay", delay_info.total_delay)
        if delay_info.before_qmgr is not None:
            span.set_attribute(
                "mail.delay_before_qmgr", delay_info.before_qmgr
            )
        if delay_info.in_qmgr is not None:
            span.set_attribute("mail.delay_in_qmgr", delay_info.in_qmgr)
        if delay_info.conn_setup is not None:
            span.set_attribute("mail.delay_conn_setup", delay_info.conn_setup)
        if delay_info.transmission is not None:
            span.set_attribute(
                "mail.delay_transmission", delay_info.transmission
            )
