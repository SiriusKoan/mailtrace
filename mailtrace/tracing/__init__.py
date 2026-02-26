import logging
from datetime import datetime, timedelta
from time import sleep, time
from typing import Dict

from opentelemetry import trace

from mailtrace.config import Config
from mailtrace.parser import LogEntry
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

        # Buffer: message_id -> (accumulated logs, round number of last seen log)
        self._pending: dict[str, tuple[list[LogEntry], int]] = {}
        self._current_round: int = 0

    @staticmethod
    def _log_key(log: LogEntry) -> tuple:
        """Return a hashable identity key for a log entry.

        The combination of timestamp + hostname + service + message uniquely
        identifies a log line, which is what we use to detect duplicates that
        arise from the go_back_seconds query overlap.
        """
        return (log.datetime, log.hostname, log.service, log.message)

    def _accumulate_logs(
        self, logs_by_message_id: Dict[str, list[LogEntry]]
    ) -> None:
        """Merge freshly queried logs into the pending buffer.

        For each message ID in the new batch, append only logs that are not
        already buffered (dedup by identity key) and refresh the "last seen"
        round counter so that the hold-round window restarts from the current
        iteration.  Duplicates arise naturally from the go_back_seconds overlap
        where the same log is returned by two consecutive queries.
        """
        for message_id, new_logs in logs_by_message_id.items():
            if message_id in self._pending:
                existing_logs, _ = self._pending[message_id]
                seen_keys = {self._log_key(log) for log in existing_logs}
                deduped = [
                    log
                    for log in new_logs
                    if self._log_key(log) not in seen_keys
                ]
                added = len(deduped)
                skipped = len(new_logs) - added
                if skipped:
                    logger.debug(
                        f"Deduped {skipped} duplicate log(s) for message_id {message_id}"
                    )
                self._pending[message_id] = (
                    existing_logs + deduped,
                    self._current_round,
                )
            else:
                self._pending[message_id] = (new_logs, self._current_round)

    def _collect_ready(self) -> Dict[str, list[LogEntry]]:
        """Return message IDs whose logs are ready to be exported.

        A message ID is considered ready when no new logs for it have been seen
        for at least ``hold_rounds`` consecutive rounds, meaning the last-seen
        round is at least ``hold_rounds`` behind the current round.

        Ready entries are removed from the pending buffer.
        """
        hold_rounds = self.config.tracing.hold_rounds
        ready: Dict[str, list[LogEntry]] = {}
        stale_ids = [
            mid
            for mid, (_, last_seen) in self._pending.items()
            if self._current_round - last_seen >= hold_rounds
        ]
        for mid in stale_ids:
            ready[mid] = self._pending.pop(mid)[0]
        return ready

    def _export_traces(
        self, logs_by_message_id: Dict[str, list[LogEntry]]
    ) -> int:
        """Parse logs and export OTel traces for the given message-ID groups.

        Returns the number of traces successfully exported.
        """
        trace_count = 0

        for message_id, message_id_logs in logs_by_message_id.items():
            hosts_logs = group_logs_by_hosts(message_id_logs)
            hosts = list(hosts_logs.keys())
            logger.debug(
                f"Processing email with message_id {message_id} and hosts {hosts}"
            )

            host_info: dict[str, tuple[DelayInfo, datetime, datetime]] = {}

            # For each host, detect the MTA, parse the logs to extract delay info,
            # and determine the start and end times for the host span
            for host, host_logs in hosts_logs.items():
                mta = detect_mta_from_entries(host_logs)
                parser = get_parser_for_mta(mta)
                delay_info = DelayInfo()
                for log in host_logs:
                    delay_info |= parser.parse(log.message)
                logger.debug(f"Host {host} has delay info: {delay_info}")

                # Start time: first log entry datetime
                host_start = min(
                    datetime.fromisoformat(log.datetime.replace("Z", "+00:00"))
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

            # Create root span
            root_span = create_root_span(message_id, root_start)
            root_ctx = trace.set_span_in_context(root_span)

            # Create host spans and their child delay spans
            for host, (delays, host_start, host_end) in host_info.items():
                host_span = create_host_span(host, host_start, root_ctx)
                host_ctx = trace.set_span_in_context(host_span)

                # Create delay stage spans (siblings under the host span)
                create_delay_spans(delays, host, host_start, host_ctx)

                logger.debug(
                    f"Close host span: {host} at {host_end.isoformat()}"
                )
                host_span.end(end_time=dt_to_ns(host_end))

            # End the root span last
            root_span.end(end_time=int(root_end.timestamp() * 1e9))

        return trace_count

    def run(self) -> None:
        sleep_seconds = self.config.tracing.sleep_seconds
        hold_rounds = self.config.tracing.hold_rounds
        try:
            while True:
                self.timing.start()
                self._current_round += 1

                # Query new logs for this iteration window.
                # Start slightly before last_query_time so that logs whose
                # syslog timestamp predates the OpenSearch ingest time are not
                # missed.  Duplicates introduced by the overlap are dropped in
                # _accumulate_logs.
                query_end = datetime.utcnow()
                go_back = timedelta(
                    seconds=self.config.tracing.go_back_seconds
                )
                query_start = self.last_query_time - go_back
                logs = query_all_logs(self.config, query_start, query_end)
                self.timing.mark("query_logs")

                # Accumulate new logs into the per-message-ID buffer, refreshing
                # the last-seen round for any ID that appeared in this batch
                new_logs_by_message_id = group_logs_by_message_id(logs)
                self._accumulate_logs(new_logs_by_message_id)

                logger.debug(
                    f"Round {self._current_round}: {len(new_logs_by_message_id)} message IDs in new batch, "
                    f"{len(self._pending)} total buffered (hold_rounds={hold_rounds})"
                )

                # Only export traces for IDs that have been quiet for hold_rounds
                ready_logs = self._collect_ready()
                trace_count = 0
                if ready_logs:
                    logger.debug(
                        f"Exporting {len(ready_logs)} ready message ID(s): {list(ready_logs.keys())}"
                    )
                    trace_count = self._export_traces(ready_logs)
                self.timing.mark("create_spans")

                # Emit the traces to the OpenTelemetry collector
                flush_traces()
                self.timing.mark("flush_traces")

                # Print timing summary if we exported any traces
                if trace_count > 0:
                    self.timing.set_trace_count(trace_count)
                    self.timing.print_summary()

                # Update the last query time to the end of the current query
                self.last_query_time = query_end
                sleep(sleep_seconds)
        except KeyboardInterrupt:
            print("EmailTracesGenerator stopped.")


__all__ = ["EmailTracesGenerator"]
