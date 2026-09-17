import logging
from datetime import datetime, timedelta
from time import sleep, time
from typing import Dict

from mailtrace.config import Config
from mailtrace.parser import LogEntry
from mailtrace.tracing.builder import export_traces
from mailtrace.tracing.lifecycle import PendingTrace, should_export_trace
from mailtrace.tracing.otel import flush_traces, init_exporter
from mailtrace.tracing.query import (
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

        # Buffer message logs until a terminal outcome or the age limit.
        self._pending: dict[str, PendingTrace] = {}
        # Keep queue-to-message associations across query rounds.
        self._queue_id_to_message_id: dict[tuple[str, str], str] = {}
        self._current_round: int = 0
        self._total_traces: int = 0

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
        already buffered and refresh the observation round only for new logs.
        Duplicates arise naturally from the go_back_seconds overlap.
        """
        for message_id, new_logs in logs_by_message_id.items():
            pending = self._pending.get(message_id)
            if pending is None:
                self._pending[message_id] = PendingTrace(
                    logs=list(new_logs),
                    first_seen_round=self._current_round,
                    last_seen_round=self._current_round,
                    has_terminal_outcome=any(
                        log.is_terminal is True for log in new_logs
                    ),
                )
                continue

            added = pending.merge(new_logs, self._current_round, self._log_key)
            skipped = len(new_logs) - added
            if skipped:
                logger.debug(
                    f"Deduped {skipped} duplicate log(s) for message_id {message_id}"
                )

    def _collect_ready(self) -> Dict[str, list[LogEntry]]:
        """Return message IDs whose logs are ready to be exported.

        A message ID is ready after a terminal outcome has been quiet for
        ``hold_rounds`` rounds, or after the configured maximum trace age.

        Ready entries are removed from the pending buffer.
        """
        hold_rounds = self.config.tracing.hold_rounds
        sleep_seconds = self.config.tracing.sleep_seconds
        max_trace_age_seconds = self.config.tracing.max_trace_age_seconds
        ready: Dict[str, list[LogEntry]] = {}
        stale_ids = [
            mid
            for mid, pending in self._pending.items()
            if should_export_trace(
                pending,
                self._current_round,
                sleep_seconds,
                hold_rounds,
                max_trace_age_seconds,
            )
        ]
        for mid in stale_ids:
            ready[mid] = self._pending.pop(mid).logs
        stale_message_ids = set(stale_ids)
        for queue_key, message_id in list(
            self._queue_id_to_message_id.items()
        ):
            if message_id in stale_message_ids:
                del self._queue_id_to_message_id[queue_key]
        return ready

    def _export_traces(
        self, logs_by_message_id: Dict[str, list[LogEntry]]
    ) -> int:
        """Build and export traces for the provided message-ID groups."""
        return export_traces(logs_by_message_id)

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
                new_logs_by_message_id = group_logs_by_message_id(
                    logs,
                    self._queue_id_to_message_id,
                )
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
                    self._total_traces += trace_count

                # Update the last query time to the end of the current query
                self.last_query_time = query_end
                sleep(sleep_seconds)
        except KeyboardInterrupt:
            print("EmailTracesGenerator stopped.")
            print(f"Total traces generated: {self._total_traces}")


__all__ = ["EmailTracesGenerator"]
