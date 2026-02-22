"""CLI command for continuous tracing from OpenSearch."""

import logging

from mailtrace.config import Config
from mailtrace.tracing import EmailTracesGenerator

logger = logging.getLogger("mailtrace")


def run_continuous_tracing(
    config: Config,
    otel_endpoint: str,
    interval_seconds: int,
) -> None:
    """Run continuous tracing by querying logs and generating traces.

    Args:
        config: Configuration object with OpenSearch settings
        otel_endpoint: OpenTelemetry OTLP endpoint for sending traces
        interval_seconds: Interval in seconds between log queries
    """
    # Create tracer instance with all tracing logic encapsulated
    tracer = EmailTracesGenerator(
        config=config,
        otel_endpoint=otel_endpoint,
    )

    # Run continuous tracing - this handles everything internally
    tracer.run(interval_seconds=interval_seconds)
