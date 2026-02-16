"""OpenTelemetry OTLP exporter setup."""

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)


def setup_otel_exporter(endpoint: str) -> OTLPSpanExporter:
    """Setup OpenTelemetry OTLP exporter.

    Args:
        endpoint: The OTLP endpoint URL

    Returns:
        Configured OTLPSpanExporter instance
    """
    return OTLPSpanExporter(
        endpoint=endpoint,
        insecure=True,
    )
