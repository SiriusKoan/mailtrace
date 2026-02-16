"""OpenTelemetry tracer creation and provider setup."""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def create_tracer_for_host(
    hostname: str, exporter: OTLPSpanExporter
) -> tuple[trace.Tracer, TracerProvider]:
    """Create a tracer instance for a specific hostname.

    Args:
        hostname: The hostname to create tracer for
        exporter: The OTLP exporter to use

    Returns:
        Tuple of (tracer, provider)
    """
    resource = Resource(
        attributes={"service.name": hostname, "service.version": "1.0.0"}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider.get_tracer(__name__), provider


def get_root_span_tracer(
    exporter: OTLPSpanExporter,
) -> tuple[trace.Tracer, TracerProvider]:
    """Create a tracer for the root span with service.name=mailtrace.

    Args:
        exporter: The OTLP exporter to use

    Returns:
        Tuple of (tracer, provider)
    """
    resource = Resource(
        attributes={"service.name": "mailtrace", "service.version": "1.0.0"}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider.get_tracer(__name__), provider
