import logging
import re
from datetime import datetime, timedelta
from time import sleep, time
from typing import Dict, Iterable, Optional

from opentelemetry import trace

from mailtrace.config import Config
from mailtrace.models import DeliveryStatus
from mailtrace.parser import LogEntry
from mailtrace.tracing.delay_parser import (
    DelayInfo,
    detect_mta_from_entries,
    get_parser_for_mta,
)
from mailtrace.tracing.lifecycle import PendingTrace, should_export_trace
from mailtrace.tracing.otel import (
    MIN_SPAN_DURATION_SECONDS,
    create_delay_spans,
    create_delivery_span,
    create_host_span,
    create_root_span,
    dt_to_ns,
    flush_traces,
    get_effective_total_delay,
    init_exporter,
    mark_span_failed,
)
from mailtrace.tracing.query import (
    HopKey,
    build_hop_handoffs,
    build_hop_links,
    group_logs_by_hops,
    group_logs_by_message_id,
    query_all_logs,
)

logger = logging.getLogger("mailtrace")

_EXIM_DELIVERY_RECIPIENT_RE = re.compile(
    r"\s(?:=>|->|\*\*)\s+([^\s<>:]+@[^\s<>:]+)"
)


def _join_unique(values: Iterable[str | None]) -> Optional[str]:
    unique_values = list(dict.fromkeys(value for value in values if value))
    return ",".join(unique_values) if unique_values else None


def _mark_failure_from_log(span: trace.Span, log: LogEntry) -> None:
    mark_span_failed(
        span,
        log.delivery_status.value,
        mail_status=log.mail_status,
        smtp_response_code=log.smtp_code,
        smtp_enhanced_status_code=log.smtp_enhanced_status_code,
    )


def _select_failure(logs: Iterable[LogEntry]) -> LogEntry | None:
    temporary_failure = None
    for log in logs:
        if log.delivery_status is DeliveryStatus.PERMANENT_FAILURE:
            return log
        if log.delivery_status is DeliveryStatus.TEMPORARY_FAILURE:
            temporary_failure = temporary_failure or log
    return temporary_failure


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

    def _extract_sender_recipient(
        self, logs: list[LogEntry]
    ) -> tuple[Optional[str], Optional[list[str]]]:
        """Extract sender and recipients from email logs.

        Args:
            logs: List of log entries for an email trace.

        Returns:
            Tuple of (sender, recipients), sender may be None, recipients is a list (possibly empty).
        """
        sender: Optional[str] = None
        recipients: list[str] = []
        seen_recipients = set()

        for log in logs:
            if not sender:
                from_match = re.search(r"from=<([^>]*)>", log.message)
                if from_match:
                    sender = from_match.group(1)

            # Extract all recipients from this log
            to_match = re.search(r"to=<([^>]*)>", log.message)
            if to_match:
                recipient = to_match.group(1)
                if recipient not in seen_recipients:
                    recipients.append(recipient)
                    seen_recipients.add(recipient)

            exim_delivery_match = _EXIM_DELIVERY_RECIPIENT_RE.search(
                log.message
            )
            if exim_delivery_match:
                recipient = exim_delivery_match.group(1)
                if recipient not in seen_recipients:
                    recipients.append(recipient)
                    seen_recipients.add(recipient)

        return sender, recipients if recipients else None

    def _export_traces(
        self, logs_by_message_id: Dict[str, list[LogEntry]]
    ) -> int:
        """Parse logs and export OTel traces for the given message-ID groups.

        Returns the number of traces successfully exported.
        """
        trace_count = 0

        for message_id, message_id_logs in logs_by_message_id.items():
            hops_logs = group_logs_by_hops(message_id_logs)
            hop_handoffs = build_hop_handoffs(hops_logs)
            hop_links = build_hop_links(hops_logs)
            hop_parents = {
                child_hop: parent_hop
                for child_hop, (parent_hop, _) in hop_links.items()
            }
            logger.debug(
                f"Processing email with message_id {message_id} and hops {list(hops_logs.keys())}"
            )

            hop_info: dict[
                HopKey,
                tuple[
                    list[
                        tuple[
                            DelayInfo,
                            datetime,
                            Optional[str],
                            LogEntry,
                        ]
                    ],
                    datetime,
                    datetime,
                ],
            ] = {}

            # Each queue ID is a separate hop, even when two queues share a host.
            for hop, host_logs in hops_logs.items():
                host, _ = hop
                mta = detect_mta_from_entries(host_logs)
                parser = get_parser_for_mta(mta)
                delay_records: list[
                    tuple[
                        DelayInfo,
                        datetime,
                        Optional[str],
                        LogEntry,
                    ]
                ] = []
                for log in host_logs:
                    parsed_delay = parser.parse(log.message)
                    if parsed_delay.get_delay_values():
                        delay_end = datetime.fromisoformat(
                            log.datetime.replace("Z", "+00:00")
                        )
                        delay_start = delay_end - timedelta(
                            seconds=get_effective_total_delay(parsed_delay)
                        )
                        _, delivery_recipients = (
                            self._extract_sender_recipient([log])
                        )
                        recipient = (
                            delivery_recipients[0]
                            if delivery_recipients
                            else None
                        )
                        delay_records.append(
                            (parsed_delay, delay_start, recipient, log)
                        )
                logger.debug(
                    f"Hop {hop} has {len(delay_records)} delay records"
                )

                if delay_records:
                    host_start = min(start for _, start, _, _ in delay_records)
                    host_end = max(
                        start
                        + timedelta(seconds=get_effective_total_delay(delays))
                        for delays, start, _, _ in delay_records
                    )
                else:
                    log_times = [
                        datetime.fromisoformat(
                            log.datetime.replace("Z", "+00:00")
                        )
                        for log in host_logs
                    ]
                    host_start = min(log_times)
                    host_end = max(log_times)
                hop_info[hop] = (delay_records, host_start, host_end)

            if not hop_info:
                logger.debug(
                    f"No delay info found for message_id {message_id}, skipping"
                )
                continue

            hop_placements: dict[
                HopKey,
                tuple[
                    list[tuple[HopKey, LogEntry]],
                    Optional[bool],
                ],
            ] = {}
            hops_by_host: dict[str, list[HopKey]] = {}
            for hop in hop_info:
                normalized_host = hop[0].rstrip(".").split(".", 1)[0].lower()
                hops_by_host.setdefault(normalized_host, []).append(hop)
            for host_hops in hops_by_host.values():
                host_hops.sort(
                    key=lambda hop: (
                        hop_info[hop][1],
                        hop_info[hop][2],
                        hop[1],
                    )
                )
                previous_hop = None
                for hop in host_hops:
                    explicit_handoffs = hop_handoffs.get(hop)
                    if explicit_handoffs:
                        hop_placements[hop] = (
                            explicit_handoffs,
                            True,
                        )
                    elif previous_hop is not None:
                        previous_handoffs, _ = hop_placements[previous_hop]
                        hop_placements[hop] = (
                            previous_handoffs,
                            False,
                        )
                    else:
                        hop_placements[hop] = ([], None)
                    previous_hop = hop

            hop_dependencies = {
                hop: {parent_hop for parent_hop, _ in handoffs}
                for hop, (handoffs, _) in hop_placements.items()
            }
            pending_hops = list(hop_info)
            ordered_hops: list[HopKey] = []
            while pending_hops:
                ready_hops = [
                    hop
                    for hop in pending_hops
                    if not hop_dependencies[hop].intersection(pending_hops)
                ]
                if not ready_hops:
                    logger.warning(
                        f"Cycle detected in hop graph for {message_id}; "
                        "attaching remaining hops to root"
                    )
                    for hop in pending_hops:
                        hop_parents.pop(hop, None)
                        hop_links.pop(hop, None)
                        hop_placements[hop] = ([], None)
                        hop_dependencies[hop].clear()
                    ready_hops = pending_hops.copy()
                for hop in ready_hops:
                    pending_hops.remove(hop)
                    ordered_hops.append(hop)

            hop_offsets: dict[HopKey, timedelta] = {}
            hop_bounds: dict[HopKey, tuple[datetime, datetime]] = {}
            for hop in ordered_hops:
                _, raw_start, raw_end = hop_info[hop]
                parent_hop = hop_parents.get(hop)
                offset = timedelta(0)
                if parent_hop is not None:
                    _, handoff_log = hop_links[hop]
                    parent_records = hop_info[parent_hop][0]
                    handoff_record = next(
                        (
                            (delays, start)
                            for delays, start, _, log in parent_records
                            if log is handoff_log
                        ),
                        None,
                    )
                    if handoff_record is not None:
                        delays, start = handoff_record
                        stage_durations = list(
                            delays.get_delay_values().values()
                        )
                        handoff_start = start + hop_offsets[parent_hop]
                        handoff_start += timedelta(
                            seconds=sum(
                                max(
                                    duration,
                                    MIN_SPAN_DURATION_SECONDS,
                                )
                                for duration in stage_durations[:-1]
                            )
                        )
                        offset = handoff_start - raw_start
                    else:
                        handoff_start = datetime.fromisoformat(
                            handoff_log.datetime.replace("Z", "+00:00")
                        )
                        parent_start = hop_bounds[parent_hop][0]
                        if handoff_start <= parent_start:
                            handoff_start = parent_start + timedelta(
                                seconds=MIN_SPAN_DURATION_SECONDS
                            )
                        offset = handoff_start - raw_start
                hop_offsets[hop] = offset
                hop_bounds[hop] = (raw_start + offset, raw_end + offset)

            trace_count += 1

            # Root span covers the full delivery window across all hosts
            root_start = min(bounds[0] for bounds in hop_bounds.values())
            root_end = max(bounds[1] for bounds in hop_bounds.values())

            # Extract sender and recipients from logs
            sender, recipients = self._extract_sender_recipient(
                message_id_logs
            )

            # Create root span with sender and recipients attributes
            root_span = create_root_span(
                message_id, root_start, sender=sender, recipients=recipients
            )
            if any(
                log.delivery_status is DeliveryStatus.PERMANENT_FAILURE
                for log in message_id_logs
            ):
                mark_span_failed(
                    root_span, DeliveryStatus.PERMANENT_FAILURE.value
                )
            root_ctx = trace.set_span_in_context(root_span)

            # Create upstream hops before their downstream branches.
            pending_hops = ordered_hops.copy()
            host_contexts: dict[HopKey, list[object]] = {}
            delivery_host_contexts: dict[int, object] = {}
            handoff_contexts: dict[int, object] = {}
            while pending_hops:
                progressed = False
                for hop in pending_hops.copy():
                    parent_hop = hop_parents.get(hop)
                    if parent_hop in pending_hops:
                        continue

                    host, host_queue_id = hop
                    delay_records, _, _ = hop_info[hop]
                    host_start, host_end = hop_bounds[hop]
                    incoming_handoffs, explicit_handoff = hop_placements[hop]
                    records_by_recipient: dict[
                        Optional[str],
                        list[
                            tuple[
                                DelayInfo,
                                datetime,
                                Optional[str],
                                LogEntry,
                            ]
                        ],
                    ] = {}
                    for record in delay_records:
                        records_by_recipient.setdefault(record[2], []).append(
                            record
                        )
                    if not records_by_recipient:
                        records_by_recipient[None] = []

                    host_sender, all_host_recipients = (
                        self._extract_sender_recipient(hops_logs[hop])
                    )
                    delivery_logs = [record[3] for record in delay_records]
                    host_smtp_response_code = next(
                        (
                            log.smtp_code
                            for log in hops_logs[hop]
                            if log.smtp_code is not None
                        ),
                        None,
                    )
                    delivery_log_ids = {id(log) for log in delivery_logs}
                    host_failure = _select_failure(
                        log
                        for log in hops_logs[hop]
                        if id(log) not in delivery_log_ids
                    )
                    for (
                        recipient,
                        branch_records,
                    ) in records_by_recipient.items():
                        selected_handoff = None
                        if len(incoming_handoffs) == 1:
                            selected_handoff = incoming_handoffs[0]
                        elif incoming_handoffs:
                            matching_handoffs = []
                            for handoff in incoming_handoffs:
                                _, handoff_recipients = (
                                    self._extract_sender_recipient(
                                        [handoff[1]]
                                    )
                                )
                                if recipient in (handoff_recipients or []):
                                    matching_handoffs.append(handoff)
                            if matching_handoffs:
                                selected_handoff = matching_handoffs[0]
                            else:
                                selected_handoff = incoming_handoffs[0]
                                logger.warning(
                                    "Ambiguous handoff for hop %s recipient %s; "
                                    "using the first observed edge",
                                    hop,
                                    recipient,
                                )

                        if selected_handoff is None:
                            parent_context = root_ctx
                        else:
                            selected_parent, handoff_log = selected_handoff
                            parent_context = handoff_contexts.get(
                                id(handoff_log)
                            ) or delivery_host_contexts.get(id(handoff_log))
                            if parent_context is None:
                                parent_context = host_contexts[
                                    selected_parent
                                ][0]

                        branch_logs = (
                            [record[3] for record in branch_records]
                            if len(records_by_recipient) > 1
                            else hops_logs[hop]
                        )
                        host_recipients = (
                            [recipient]
                            if recipient is not None
                            else all_host_recipients
                        )
                        host_next_queue_id = _join_unique(
                            log.queued_as for log in branch_logs
                        )
                        host_relay_host = _join_unique(
                            log.relay_host for log in branch_logs
                        )
                        host_relay_ip = _join_unique(
                            log.relay_ip for log in branch_logs
                        )
                        host_relay_port = next(
                            (
                                log.relay_port
                                for log in branch_logs
                                if log.relay_port is not None
                            ),
                            None,
                        )
                        host_next_host = next(
                            (
                                log.relay_host
                                for log in branch_logs
                                if log.relay_host
                            ),
                            None,
                        )
                        host_span = create_host_span(
                            host,
                            host_start,
                            parent_context,
                            message_id=message_id,
                            sender=host_sender,
                            recipients=host_recipients,
                            queue_id=host_queue_id,
                            next_host=host_next_host,
                            explicit_handoff=(explicit_handoff),
                            next_queue_id=host_next_queue_id,
                            relay_host=host_relay_host,
                            relay_ip=host_relay_ip,
                            relay_port=host_relay_port,
                            smtp_response_code=host_smtp_response_code,
                        )
                        if host_failure is not None:
                            _mark_failure_from_log(host_span, host_failure)
                        host_ctx = trace.set_span_in_context(host_span)
                        host_contexts.setdefault(hop, []).append(host_ctx)

                        for _, _, _, log in branch_records:
                            delivery_host_contexts[id(log)] = host_ctx

                        for (
                            delays,
                            delay_start,
                            delivery_recipient,
                            log,
                        ) in branch_records:
                            delay_start += hop_offsets[hop]
                            delivery_span = create_delivery_span(
                                host,
                                delay_start,
                                host_ctx,
                                recipient=delivery_recipient,
                                transport=log.service,
                            )
                            if log.delivery_status in {
                                DeliveryStatus.TEMPORARY_FAILURE,
                                DeliveryStatus.PERMANENT_FAILURE,
                            }:
                                _mark_failure_from_log(delivery_span, log)
                            delivery_ctx = trace.set_span_in_context(
                                delivery_span
                            )
                            delay_spans = create_delay_spans(
                                delays, host, delay_start, delivery_ctx
                            )
                            if delay_spans:
                                handoff_contexts[id(log)] = (
                                    trace.set_span_in_context(delay_spans[-1])
                                )
                            delivery_end = delay_start + timedelta(
                                seconds=get_effective_total_delay(delays)
                            )
                            delivery_span.end(end_time=dt_to_ns(delivery_end))

                        logger.debug(
                            "Close host span: %s recipient %s at %s",
                            hop,
                            recipient,
                            host_end.isoformat(),
                        )
                        host_span.end(end_time=dt_to_ns(host_end))
                    pending_hops.remove(hop)
                    progressed = True

                if not progressed:
                    logger.warning(
                        f"Cycle detected in hop graph for {message_id}; "
                        "attaching remaining hops to root"
                    )
                    for hop in pending_hops:
                        hop_parents.pop(hop, None)

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
