"""Core tracing module for continuous trace generation from OpenSearch.

This module contains the core logic for:
- Querying logs from OpenSearch (query.py)
- Modeling email traces (models.py)
- Parsing delay information (delay_parser.py)
- Building delays from log entries (delay_builder.py)
- Setting up OpenTelemetry and generating spans (otel/)
- Continuous trace generation (continuous.py)

The CLI layer (mailtrace.cli.tracing) should use the ContinuousTracer class
to encapsulate all tracing logic. This separation mirrors the pattern used by
mailtrace.aggregator (core) vs mailtrace.cli.run (CLI glue).

Module structure:
- models.py: EmailTrace and Delay classes for representing email traces
- delay_parser.py: Extensible delay parsing with PostfixDelayParser
- delay_builder.py: DelayBuilder for creating delays from entries
- otel/: OpenTelemetry setup, tracer creation, and span generation
- query.py: OpenSearch querying and log grouping by message ID
- continuous.py: ContinuousTracer class for continuous trace generation

Public API:
- ContinuousTracer: Main class for continuous tracing (recommended)
- Lower-level functions are also exported for advanced use cases
"""

from mailtrace.tracing.continuous import ContinuousTracer
from mailtrace.tracing.models import Delay, EmailTrace
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
    # Main API
    "ContinuousTracer",
    # Models
    "EmailTrace",
    "Delay",
    # Lower-level OpenTelemetry functions
    "setup_otel_exporter",
    "create_tracer_for_host",
    "get_root_span_tracer",
    "generate_trace_from_email",
    "message_id_to_trace_id",
    # Query functions
    "query_logs_from_all_hosts",
    "group_logs_by_message_id",
]
