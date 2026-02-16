"""Continuous trace generation from OpenSearch logs.

This module provides the ContinuousTracer class that encapsulates all
tracing logic, including:
- OpenTelemetry setup and tracer management
- Log querying and grouping
- Trace generation and span creation
- Provider lifecycle management

The CLI layer should only need to create a ContinuousTracer instance
and call run() to start continuous tracing.
"""

import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from opentelemetry.trace import Tracer

from mailtrace.config import Config
from mailtrace.tracing.otel import (
    create_tracer_for_host,
    generate_trace_from_email,
    get_root_span_tracer,
    setup_otel_exporter,
)
from mailtrace.tracing.query import (
    group_logs_by_message_id,
    query_logs_from_all_hosts,
)

logger = logging.getLogger("mailtrace")


class ContinuousTracer:
    """Manages continuous trace generation from OpenSearch logs.

    This class encapsulates all the complexity of:
    - Setting up OpenTelemetry exporters and tracers
    - Querying logs at regular intervals
    - Grouping logs by message ID
    - Generating traces with proper parent-child relationships
    - Managing tracer provider lifecycle

    Example:
        >>> from mailtrace.config import Config
        >>> config = Config.from_file("config.yaml")
        >>> tracer = ContinuousTracer(
        ...     config=config,
        ...     otel_endpoint="http://localhost:4318"
        ... )
        >>> tracer.run(interval_seconds=60)
    """

    def __init__(self, config: Config, otel_endpoint: str):
        """Initialize the continuous tracer.

        Args:
            config: Configuration object with OpenSearch settings
            otel_endpoint: OpenTelemetry OTLP endpoint for sending traces
        """
        self.config = config
        self.otel_endpoint = otel_endpoint

        # OpenTelemetry components - must be set in _setup_otel()
        self._exporter: Optional[Any] = None  # OTLPSpanExporter
        self._root_tracer: Optional[Any] = None  # Tracer
        self._root_provider: Optional[Any] = None  # TracerProvider
        self._host_tracers_cache: Dict[str, Tuple[Any, Any]] = {}

        # Tracking state
        self._last_query_time: Optional[datetime] = None
        self._is_running = False

    def _setup_otel(self) -> None:
        """Set up OpenTelemetry exporter and root tracer."""
        logger.info(
            f"Setting up OpenTelemetry with endpoint: {self.otel_endpoint}"
        )
        self._exporter = setup_otel_exporter(self.otel_endpoint)
        self._root_tracer, self._root_provider = get_root_span_tracer(
            self._exporter
        )
        logger.info("OpenTelemetry tracer initialized")

    def _get_tracer_for_host(self, hostname: str) -> Any:
        """Get or create a tracer for a specific host.

        Args:
            hostname: The hostname to get a tracer for

        Returns:
            Tracer instance for the host
        """
        if hostname not in self._host_tracers_cache:
            # _exporter is guaranteed to be set by _setup_otel() before this is called
            if self._exporter is None:
                raise RuntimeError(
                    "_exporter must be initialized before _get_tracer_for_host"
                )
            tracer, host_provider = create_tracer_for_host(
                hostname, self._exporter
            )
            self._host_tracers_cache[hostname] = (tracer, host_provider)

        return self._host_tracers_cache[hostname][0]

    def _build_tracers_by_host(self, traces: Dict) -> Dict[str, Tracer]:
        """Build a mapping of hostname -> tracer for all hosts in traces.

        Args:
            traces: Dictionary of message_id -> EmailTrace

        Returns:
            Dictionary mapping hostname -> tracer
        """
        tracers_by_host = {}

        for email_trace in traces.values():
            for entry in email_trace.entries:
                hostname = entry.hostname
                if hostname not in tracers_by_host:
                    tracers_by_host[hostname] = self._get_tracer_for_host(
                        hostname
                    )

        return tracers_by_host

    def _query_and_generate_traces(self) -> None:
        """Query logs and generate traces for one iteration."""
        logger.info("Querying logs...")
        current_time = datetime.utcnow()

        # Query logs from last_query_time to current_time
        # _last_query_time is guaranteed to be set in run() before this is called
        if self._last_query_time is None:
            raise RuntimeError(
                "_last_query_time must be initialized before _query_and_generate_traces"
            )
        start_time_str = self._last_query_time.strftime("%Y-%m-%dT%H:%M:%S")
        end_time_str = current_time.strftime("%Y-%m-%dT%H:%M:%S")

        logs = query_logs_from_all_hosts(
            self.config, start_time_str, end_time_str
        )
        logger.info(f"Retrieved {len(logs)} log entries")

        # Group by message ID across all hops
        traces = group_logs_by_message_id(logs)
        logger.info(f"Found {len(traces)} unique email traces")

        # Build tracers_by_host mapping from all unique hostnames in traces
        tracers_by_host = self._build_tracers_by_host(traces)

        # Generate traces
        trace_generation_start = time.time()
        trace_count = 0

        # _root_tracer is guaranteed to be set by _setup_otel() before this is called
        if self._root_tracer is None:
            raise RuntimeError(
                "_root_tracer must be initialized before _query_and_generate_traces"
            )

        for message_id, email_trace in traces.items():
            try:
                generate_trace_from_email(
                    self._root_tracer, tracers_by_host, email_trace
                )
                trace_count += 1
                logger.debug(f"Generated trace for message ID: {message_id}")
            except Exception as e:
                logger.error(f"Error generating trace for {message_id}: {e}")

        trace_generation_time = time.time() - trace_generation_start

        if trace_count > 0:
            avg_time = trace_generation_time / trace_count
            logger.info(
                f"Trace generation completed: {trace_count} traces generated "
                f"in {trace_generation_time:.3f} seconds "
                f"({avg_time:.3f}s per trace)"
            )
        else:
            logger.info(
                f"Trace generation completed: 0 traces generated "
                f"in {trace_generation_time:.3f} seconds"
            )

        # Update last query time for next iteration
        self._last_query_time = current_time

    def _shutdown_providers(self) -> None:
        """Shutdown all tracer providers to flush remaining spans."""
        logger.info("Shutting down tracer providers...")

        if self._root_provider:
            self._root_provider.shutdown()

        for _, (_, host_provider) in self._host_tracers_cache.items():
            host_provider.shutdown()

        logger.info("All tracer providers shut down")

    def run(self, interval_seconds: int = 60) -> None:
        """Run continuous tracing by querying logs and generating traces.

        This method will run indefinitely until interrupted (Ctrl+C).

        Args:
            interval_seconds: Interval in seconds between log queries
                (default: 60)
        """
        logger.info("Starting continuous tracing generation...")
        logger.info(f"Query interval: {interval_seconds} seconds")

        # Setup OpenTelemetry
        self._setup_otel()

        # Initialize query time tracking - this ensures _last_query_time is set
        # before _query_and_generate_traces() is called
        self._last_query_time = datetime.utcnow()
        self._is_running = True

        try:
            while self._is_running:
                self._query_and_generate_traces()

                # Wait for next interval
                logger.info(f"Waiting {interval_seconds} seconds...")
                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            logger.info("Stopping tracing generation...")
        finally:
            self._is_running = False
            self._shutdown_providers()

    def stop(self) -> None:
        """Stop the continuous tracer.

        This method can be called from another thread to gracefully stop
        the tracer.
        """
        logger.info("Stop requested")
        self._is_running = False
