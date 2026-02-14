"""Core tracing module for continuous trace generation from OpenSearch.

This module contains the core logic for:
- Querying logs from OpenSearch (query.py)
- Modeling email traces (models.py)
- Setting up OpenTelemetry and generating spans (otel.py)

The CLI layer (mailtrace.cli.tracing) imports from this module and provides
the command-line interface. This separation mirrors the pattern used by
mailtrace.aggregator (core) vs mailtrace.cli.run (CLI glue).

Module structure:
- models.py: EmailTrace class for representing complete email traces
- otel.py: OpenTelemetry setup, tracer creation, and span generation
- query.py: OpenSearch querying and log grouping by message ID
"""

from mailtrace.tracing.models import EmailTrace, ServiceStage
from mailtrace.tracing.otel import (
    create_tracer_for_host,
    generate_trace_from_email,
    get_root_span_tracer,
    message_id_to_trace_id,
    setup_otel_exporter,
)
from mailtrace.tracing.query import (
    group_logs_by_message_id,
    query_logs_from_all_hosts,
)

__all__ = [
    "EmailTrace",
    "ServiceStage",
    "setup_otel_exporter",
    "create_tracer_for_host",
    "get_root_span_tracer",
    "generate_trace_from_email",
    "message_id_to_trace_id",
    "query_logs_from_all_hosts",
    "group_logs_by_message_id",
]
