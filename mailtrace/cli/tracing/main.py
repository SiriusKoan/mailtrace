"""CLI command for continuous tracing from OpenSearch."""

import logging
import time
from datetime import datetime
from typing import Any, Dict

from mailtrace.config import Config
from mailtrace.tracing import (
    create_tracer_for_host,
    generate_trace_from_email,
    get_root_span_tracer,
    group_logs_by_message_id,
    query_logs_from_all_hosts,
    setup_otel_exporter,
)

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
    logger.info("Starting continuous tracing generation...")
    logger.info(f"OTLP endpoint: {otel_endpoint}")
    logger.info(f"Query interval: {interval_seconds} seconds")

    # Setup OpenTelemetry
    exporter = setup_otel_exporter(otel_endpoint)
    root_tracer, root_provider = get_root_span_tracer(exporter)

    # Create tracers for each unique host from config
    # Note: Tracers will be created dynamically for hosts not in config
    tracers_by_host: Dict[str, Any] = {}

    logger.info(f"Created tracers for {len(tracers_by_host)} configured hosts")

    # Track the last query time
    last_query_time = datetime.utcnow()

    try:
        while True:
            logger.info("Querying logs...")
            current_time = datetime.utcnow()

            # Query logs from last_query_time to current_time
            start_time_str = last_query_time.strftime("%Y-%m-%dT%H:%M:%S")
            end_time_str = current_time.strftime("%Y-%m-%dT%H:%M:%S")

            logs = query_logs_from_all_hosts(
                config, start_time_str, end_time_str
            )
            logger.info(f"Retrieved {len(logs)} log entries")

            # Group by message ID across all hops
            traces = group_logs_by_message_id(logs)
            logger.info(f"Found {len(traces)} unique email traces")

            # Generate traces
            for message_id, email_trace in traces.items():
                try:
                    # Create tracers dynamically for any new hosts in the email trace
                    for stage in email_trace.get_service_stages():
                        hostname = stage.hostname
                        if hostname not in tracers_by_host:
                            host_tracer, _ = create_tracer_for_host(
                                hostname, exporter
                            )
                            tracers_by_host[hostname] = host_tracer
                            logger.debug(
                                f"Created dynamic tracer for host: {hostname}"
                            )

                    generate_trace_from_email(
                        root_tracer, tracers_by_host, email_trace
                    )
                    logger.debug(
                        f"Generated trace for message ID: {message_id}"
                    )
                except Exception as e:
                    logger.error(
                        f"Error generating trace for {message_id}: {e}"
                    )

            # Update last query time for next iteration
            last_query_time = current_time

            # Wait for next interval
            logger.info(f"Waiting {interval_seconds} seconds...")
            time.sleep(interval_seconds)

    except KeyboardInterrupt:
        logger.info("Stopping continuous tracing...")
    finally:
        # Shutdown providers to flush remaining spans
        root_provider.shutdown()
