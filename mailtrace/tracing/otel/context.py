"""OpenTelemetry span context utilities and helpers."""

import secrets

from opentelemetry.trace import Link, SpanContext, TraceFlags


def message_id_to_trace_id(message_id: str) -> int:
    """Convert message ID to a valid trace ID (16 bytes / 128 bits).

    Args:
        message_id: The email message ID

    Returns:
        A consistent trace ID derived from the message ID
    """
    return abs(hash(message_id)) % (2**128)


def generate_random_span_id() -> int:
    """Generate a random span ID (8 bytes / 64 bits).

    Returns:
        A random 64-bit span ID
    """
    return secrets.randbits(64)


class SpanContextFactory:
    """Factory for creating OpenTelemetry span contexts."""

    def __init__(self, trace_id: int):
        """Initialize the factory with a trace ID.

        Args:
            trace_id: The trace ID to use for all spans
        """
        self.trace_id = trace_id

    def create_span_context(self) -> SpanContext:
        """Create a new span context with a random span ID.

        Returns:
            A new SpanContext with the configured trace ID
        """
        return SpanContext(
            trace_id=self.trace_id,
            span_id=generate_random_span_id(),
            is_remote=False,
            trace_flags=TraceFlags(0x01),  # sampled
        )

    def create_link(self) -> Link:
        """Create a link to a new span context.

        Returns:
            A Link object for establishing trace ID relationships
        """
        return Link(self.create_span_context())
