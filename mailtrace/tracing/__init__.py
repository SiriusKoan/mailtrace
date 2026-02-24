import logging
from datetime import datetime, timedelta
from time import sleep, time
from typing import Dict

from opentelemetry import trace

from mailtrace.config import Config
from mailtrace.tracing.delay_parser import (
    DelayInfo,
    detect_mta_from_entries,
    get_parser_for_mta,
)
from mailtrace.tracing.otel import (
    create_delay_spans,
    create_host_span,
    create_root_span,
    dt_to_ns,
    flush_traces,
    init_exporter,
)
from mailtrace.tracing.query import (
    group_logs_by_hosts,
    group_logs_by_message_id,
    query_all_logs,
)

logger = logging.getLogger("mailtrace")


class TimingMetrics:
    """Tracks timing information for trace generation."""

    def __init__(self):
        self.metrics: Dict[str, float] = {}
        self.start_time: float = 0
        self.trace_count: int = 0

    def start(self) -> None:
        """Start the overall timing."""
        self.start_time = time()
        self.metrics.clear()
        self.trace_count = 0

    def mark(self, step_name: str) -> None:
        """Mark the end time of a step."""
        if self.start_time == 0:
            logger.warning("Timing not started, ignoring mark")
            return
        elapsed = time() - self.start_time
        self.metrics[step_name] = elapsed

    def set_trace_count(self, count: int) -> None:
        """Set the number of traces generated."""
        self.trace_count = count

    def get_step_duration(
        self, step_name: str, previous_step: str | None = None
    ) -> float:
        """Get the duration of a specific step.

        Args:
            step_name: Name of the current step
            previous_step: Name of the previous step (if any)

        Returns:
            Duration in seconds
        """
        if step_name not in self.metrics:
            return 0.0

        current = self.metrics[step_name]
        if previous_step and previous_step in self.metrics:
            return current - self.metrics[previous_step]
        return current

    def print_summary(self) -> None:
        """Print timing summary with total and per-step durations."""
        if not self.metrics:
            logger.info("No timing metrics recorded")
            return

        total_time = self.get_step_duration(list(self.metrics.keys())[-1])

        logger.info("=" * 70)
        logger.info("TRACE GENERATION TIMING SUMMARY")
        logger.info("=" * 70)

        steps = list(self.metrics.keys())
        previous_step = None

        for step in steps:
            step_duration = self.get_step_duration(step, previous_step)
            percentage = (
                (step_duration / total_time * 100) if total_time > 0 else 0
            )
            logger.info(
                f"  {step:<40} {step_duration:>8.4f}s ({percentage:>5.1f}%)"
            )
            previous_step = step

        logger.info("-" * 70)
        logger.info(f"  {'TOTAL':<40} {total_time:>8.4f}s (100.0%)")
        if self.trace_count > 0:
            avg_time = total_time / self.trace_count
            logger.info(f"  {'Traces generated':<40} {self.trace_count:>8d}")
            logger.info(f"  {'Avg time per trace':<40} {avg_time:>8.4f}s")
        logger.info("=" * 70)


class EmailTracesGenerator:
    def __init__(self, config: Config, otel_endpoint: str) -> None:
        self.config = config
        self.otel_endpoint = otel_endpoint
        self.last_query_time = datetime.utcnow()
        self.timing = TimingMetrics()
        init_exporter(otel_endpoint)

    def run(self, interval_seconds: int) -> None:
        try:
            while True:
                self.timing.start()

                # Group logs by message_id and then by host_id
                query_end = datetime.utcnow()
                logs = query_all_logs(
                    self.config, self.last_query_time, query_end
                )
                self.timing.mark("query_logs")

                trace_count = 0

                # Iterate over all emails (grouping by message_id, then by host)
                logs_by_message_id = group_logs_by_message_id(logs)
                for message_id, message_id_logs in logs_by_message_id.items():
                    hosts_logs = group_logs_by_hosts(message_id_logs)
                    hosts = list(hosts_logs.keys())
                    logger.debug(
                        f"Processing email with message_id {message_id} and hosts {hosts}"
                    )

                    host_info: dict[
                        str, tuple[DelayInfo, datetime, datetime]
                    ] = {}

                    # For each host, detect the MTA, parse the logs to extract delay info,
                    # and determine the start and end times for the host span
                    for host, host_logs in hosts_logs.items():
                        mta = detect_mta_from_entries(host_logs)
                        parser = get_parser_for_mta(mta)
                        delay_info = DelayInfo()
                        for log in host_logs:
                            delay_info |= parser.parse(log.message)
                        logger.debug(
                            f"Host {host} has delay info: {delay_info}"
                        )

                        # Start time: first log entry datetime
                        host_start = min(
                            datetime.fromisoformat(
                                log.datetime.replace("Z", "+00:00")
                            )
                            for log in host_logs
                        )
                        # host_end = ref_time + total_delay
                        host_end = host_start + timedelta(
                            seconds=delay_info.total_delay
                        )
                        host_info[host] = (delay_info, host_start, host_end)

                    if not host_info:
                        logger.debug(
                            f"No delay info found for message_id {message_id}, skipping"
                        )
                        continue

                    trace_count += 1

                    # Root span covers the full delivery window across all hosts
                    root_start = min(info[1] for info in host_info.values())
                    root_end = max(info[2] for info in host_info.values())

                    # Create spans
                    # Create root span
                    root_span = create_root_span(message_id, root_start)
                    root_ctx = trace.set_span_in_context(root_span)

                    # Create host spans and their child delay spans
                    for host, (
                        delays,
                        host_start,
                        host_end,
                    ) in host_info.items():
                        host_span = create_host_span(
                            host, host_start, root_ctx
                        )
                        host_ctx = trace.set_span_in_context(host_span)

                        # Create delay stage spans (siblings under the host span)
                        create_delay_spans(delays, host, host_start, host_ctx)

                        logger.debug(
                            f"Close host span: {host} at {host_end.isoformat()}"
                        )
                        host_span.end(end_time=dt_to_ns(host_end))

                    # End the root span last
                    root_span.end(end_time=int(root_end.timestamp() * 1e9))

                self.timing.mark("create_spans")

                # Emit the traces to the OpenTelemetry collector
                flush_traces()
                self.timing.mark("flush_traces")

                # Print timing summary if we processed any traces
                if trace_count > 0:
                    self.timing.set_trace_count(trace_count)
                    self.timing.print_summary()

                # Update the last query time to the end of the current query
                self.last_query_time = query_end
                sleep(interval_seconds)
        except KeyboardInterrupt:
            print("EmailTracesGenerator stopped.")


__all__ = ["EmailTracesGenerator"]
