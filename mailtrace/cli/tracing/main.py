"""CLI command for continuous tracing from OpenSearch."""

import logging

from mailtrace.config import Config
from mailtrace.tracing import EmailTracesGenerator

logger = logging.getLogger("mailtrace")


def run_continuous_tracing(
    config: Config,
    otel_endpoint: str,
) -> None:
    """Run continuous tracing by querying logs and generating traces.

    Sleep duration and hold rounds are read from ``config.tracing`` so that
    they can be tuned centrally in the config file.

    Args:
        config: Configuration object with OpenSearch settings and tracing tuning
        otel_endpoint: OpenTelemetry OTLP endpoint for sending traces
    """
    logger.info(
        f"Tracing config: sleep_seconds={config.tracing.sleep_seconds}, "
        f"hold_rounds={config.tracing.hold_rounds}"
    )

    # Create tracer instance with all tracing logic encapsulated
    tracer = EmailTracesGenerator(
        config=config,
        otel_endpoint=otel_endpoint,
    )

    # Run continuous tracing - this handles everything internally
    tracer.run()
