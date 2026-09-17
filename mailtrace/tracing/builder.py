"""Build OpenTelemetry traces from mail log entries."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from opentelemetry import trace

from mailtrace.models import DeliveryStatus
from mailtrace.parser import LogEntry
from mailtrace.tracing.delay_parser import (
    DelayInfo,
    detect_mta_from_entries,
    get_parser_for_mta,
)
from mailtrace.tracing.otel import (
    MIN_SPAN_DURATION_SECONDS,
    create_delay_spans,
    create_delivery_span,
    create_host_span,
    create_root_span,
    dt_to_ns,
    get_effective_total_delay,
    mark_span_failed,
)
from mailtrace.tracing.query import (
    HopKey,
    build_hop_handoffs,
    group_logs_by_hops,
)

logger = logging.getLogger("mailtrace")

_EXIM_DELIVERY_RECIPIENT_RE = re.compile(
    r"\s(?:=>|->|\*\*)\s+([^\s<>:]+@[^\s<>:]+)"
)

Handoff = tuple[HopKey, LogEntry]


@dataclass(frozen=True)
class DeliveryRecord:
    """Data required to create one delivery span."""

    delays: DelayInfo
    start_time: datetime
    recipient: Optional[str]
    log: LogEntry


@dataclass(frozen=True)
class PreparedHop:
    """A hop whose delays and raw time bounds have been prepared."""

    logs: list[LogEntry]
    delivery_records: list[DeliveryRecord]
    raw_start: datetime
    raw_end: datetime


@dataclass(frozen=True)
class HopPlacement:
    """Upstream handoffs and inference state for one hop."""

    handoffs: tuple[Handoff, ...]
    explicit_handoff: Optional[bool]


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


def _extract_sender_recipients(
    logs: Iterable[LogEntry],
) -> tuple[Optional[str], Optional[list[str]]]:
    """Extract the sender and unique recipients in encounter order."""
    sender: Optional[str] = None
    recipients: list[str] = []
    seen_recipients = set()

    for log in logs:
        if not sender:
            from_match = re.search(r"from=<([^>]*)>", log.message)
            if from_match:
                sender = from_match.group(1)

        to_match = re.search(r"to=<([^>]*)>", log.message)
        if to_match:
            recipient = to_match.group(1)
            if recipient not in seen_recipients:
                recipients.append(recipient)
                seen_recipients.add(recipient)

        exim_delivery_match = _EXIM_DELIVERY_RECIPIENT_RE.search(log.message)
        if exim_delivery_match:
            recipient = exim_delivery_match.group(1)
            if recipient not in seen_recipients:
                recipients.append(recipient)
                seen_recipients.add(recipient)

    return sender, recipients if recipients else None


def _prepare_hops(
    hops_logs: dict[HopKey, list[LogEntry]],
) -> dict[HopKey, PreparedHop]:
    """Parse delivery delays and raw time bounds for each hop."""
    prepared_hops: dict[HopKey, PreparedHop] = {}

    for hop, host_logs in hops_logs.items():
        host, _ = hop
        parser = get_parser_for_mta(detect_mta_from_entries(host_logs))
        delivery_records: list[DeliveryRecord] = []

        for log in host_logs:
            parsed_delay = parser.parse(log.message)
            if not parsed_delay.get_delay_values():
                continue

            delay_end = datetime.fromisoformat(
                log.datetime.replace("Z", "+00:00")
            )
            delay_start = delay_end - timedelta(
                seconds=get_effective_total_delay(parsed_delay)
            )
            _, recipients = _extract_sender_recipients([log])
            delivery_records.append(
                DeliveryRecord(
                    delays=parsed_delay,
                    start_time=delay_start,
                    recipient=recipients[0] if recipients else None,
                    log=log,
                )
            )

        logger.debug("Hop %s has %d delay records", hop, len(delivery_records))

        if delivery_records:
            raw_start = min(record.start_time for record in delivery_records)
            raw_end = max(
                record.start_time
                + timedelta(seconds=get_effective_total_delay(record.delays))
                for record in delivery_records
            )
        else:
            log_times = [
                datetime.fromisoformat(log.datetime.replace("Z", "+00:00"))
                for log in host_logs
            ]
            raw_start = min(log_times)
            raw_end = max(log_times)

        prepared_hops[hop] = PreparedHop(
            logs=host_logs,
            delivery_records=delivery_records,
            raw_start=raw_start,
            raw_end=raw_end,
        )

    return prepared_hops


def _plan_hops(
    message_id: str,
    prepared_hops: dict[HopKey, PreparedHop],
) -> tuple[
    dict[HopKey, HopPlacement],
    dict[HopKey, Handoff],
    dict[HopKey, HopKey],
    list[HopKey],
]:
    """Build hop relationships in upstream-first order."""
    hops_logs = {hop: prepared.logs for hop, prepared in prepared_hops.items()}
    hop_handoffs = build_hop_handoffs(hops_logs)
    hop_links = {
        child_hop: handoffs[0] for child_hop, handoffs in hop_handoffs.items()
    }
    hop_parents = {
        child_hop: parent_hop
        for child_hop, (parent_hop, _) in hop_links.items()
    }

    hop_placements: dict[HopKey, HopPlacement] = {}
    hops_by_host: dict[str, list[HopKey]] = {}
    for hop in prepared_hops:
        normalized_host = hop[0].rstrip(".").split(".", 1)[0].lower()
        hops_by_host.setdefault(normalized_host, []).append(hop)

    for host_hops in hops_by_host.values():
        host_hops.sort(
            key=lambda hop: (
                prepared_hops[hop].raw_start,
                prepared_hops[hop].raw_end,
                hop[1],
            )
        )
        previous_hop = None
        for hop in host_hops:
            explicit_handoffs = hop_handoffs.get(hop)
            if explicit_handoffs:
                hop_placements[hop] = HopPlacement(
                    handoffs=tuple(explicit_handoffs),
                    explicit_handoff=True,
                )
            elif previous_hop is not None:
                hop_placements[hop] = HopPlacement(
                    handoffs=hop_placements[previous_hop].handoffs,
                    explicit_handoff=False,
                )
            else:
                hop_placements[hop] = HopPlacement(
                    handoffs=(),
                    explicit_handoff=None,
                )
            previous_hop = hop

    hop_dependencies = {
        hop: {parent_hop for parent_hop, _ in placement.handoffs}
        for hop, placement in hop_placements.items()
    }
    pending_hops = list(prepared_hops)
    ordered_hops: list[HopKey] = []

    while pending_hops:
        ready_hops = [
            hop
            for hop in pending_hops
            if not hop_dependencies[hop].intersection(pending_hops)
        ]
        if not ready_hops:
            logger.warning(
                "Cycle detected in hop graph for %s; "
                "attaching remaining hops to root",
                message_id,
            )
            for hop in pending_hops:
                hop_parents.pop(hop, None)
                hop_links.pop(hop, None)
                hop_placements[hop] = HopPlacement(
                    handoffs=(),
                    explicit_handoff=None,
                )
                hop_dependencies[hop].clear()
            ready_hops = pending_hops.copy()

        for hop in ready_hops:
            pending_hops.remove(hop)
            ordered_hops.append(hop)

    return hop_placements, hop_links, hop_parents, ordered_hops


def _calculate_hop_timing(
    ordered_hops: list[HopKey],
    prepared_hops: dict[HopKey, PreparedHop],
    hop_links: dict[HopKey, Handoff],
    hop_parents: dict[HopKey, HopKey],
) -> tuple[
    dict[HopKey, timedelta],
    dict[HopKey, tuple[datetime, datetime]],
]:
    """Align downstream hop times with their parent handoffs."""
    hop_offsets: dict[HopKey, timedelta] = {}
    hop_bounds: dict[HopKey, tuple[datetime, datetime]] = {}

    for hop in ordered_hops:
        prepared = prepared_hops[hop]
        parent_hop = hop_parents.get(hop)
        offset = timedelta(0)

        if parent_hop is not None:
            _, handoff_log = hop_links[hop]
            parent_records = prepared_hops[parent_hop].delivery_records
            handoff_record = next(
                (
                    record
                    for record in parent_records
                    if record.log is handoff_log
                ),
                None,
            )
            if handoff_record is not None:
                stage_durations = list(
                    handoff_record.delays.get_delay_values().values()
                )
                handoff_start = (
                    handoff_record.start_time + hop_offsets[parent_hop]
                )
                handoff_start += timedelta(
                    seconds=sum(
                        max(duration, MIN_SPAN_DURATION_SECONDS)
                        for duration in stage_durations[:-1]
                    )
                )
                offset = handoff_start - prepared.raw_start
            else:
                handoff_start = datetime.fromisoformat(
                    handoff_log.datetime.replace("Z", "+00:00")
                )
                parent_start = hop_bounds[parent_hop][0]
                if handoff_start <= parent_start:
                    handoff_start = parent_start + timedelta(
                        seconds=MIN_SPAN_DURATION_SECONDS
                    )
                offset = handoff_start - prepared.raw_start

        hop_offsets[hop] = offset
        hop_bounds[hop] = (
            prepared.raw_start + offset,
            prepared.raw_end + offset,
        )

    return hop_offsets, hop_bounds


def _group_delivery_records(
    records: list[DeliveryRecord],
) -> dict[Optional[str], list[DeliveryRecord]]:
    records_by_recipient: dict[Optional[str], list[DeliveryRecord]] = {}
    for record in records:
        records_by_recipient.setdefault(record.recipient, []).append(record)
    if not records_by_recipient:
        records_by_recipient[None] = []
    return records_by_recipient


def _select_handoff(
    hop: HopKey,
    recipient: Optional[str],
    incoming_handoffs: tuple[Handoff, ...],
) -> Handoff | None:
    if len(incoming_handoffs) == 1:
        return incoming_handoffs[0]
    if not incoming_handoffs:
        return None

    for handoff in incoming_handoffs:
        _, handoff_recipients = _extract_sender_recipients([handoff[1]])
        if recipient in (handoff_recipients or []):
            return handoff

    logger.warning(
        "Ambiguous handoff for hop %s recipient %s; "
        "using the first observed edge",
        hop,
        recipient,
    )
    return incoming_handoffs[0]


def _emit_trace(
    message_id: str,
    message_logs: list[LogEntry],
    prepared_hops: dict[HopKey, PreparedHop],
    hop_placements: dict[HopKey, HopPlacement],
    ordered_hops: list[HopKey],
    hop_offsets: dict[HopKey, timedelta],
    hop_bounds: dict[HopKey, tuple[datetime, datetime]],
) -> None:
    """Create and finish every span for one email message."""
    root_start = min(bounds[0] for bounds in hop_bounds.values())
    root_end = max(bounds[1] for bounds in hop_bounds.values())
    sender, recipients = _extract_sender_recipients(message_logs)

    root_span = create_root_span(
        message_id, root_start, sender=sender, recipients=recipients
    )
    if any(
        log.delivery_status is DeliveryStatus.PERMANENT_FAILURE
        for log in message_logs
    ):
        mark_span_failed(root_span, DeliveryStatus.PERMANENT_FAILURE.value)
    root_ctx = trace.set_span_in_context(root_span)

    host_contexts: dict[HopKey, list[object]] = {}
    delivery_host_contexts: dict[int, object] = {}
    handoff_contexts: dict[int, object] = {}

    for hop in ordered_hops:
        host, host_queue_id = hop
        prepared = prepared_hops[hop]
        host_start, host_end = hop_bounds[hop]
        placement = hop_placements[hop]
        records_by_recipient = _group_delivery_records(
            prepared.delivery_records
        )
        host_sender, all_host_recipients = _extract_sender_recipients(
            prepared.logs
        )
        delivery_log_ids = {
            id(record.log) for record in prepared.delivery_records
        }
        host_smtp_response_code = next(
            (
                log.smtp_code
                for log in prepared.logs
                if log.smtp_code is not None
            ),
            None,
        )
        host_failure = _select_failure(
            log for log in prepared.logs if id(log) not in delivery_log_ids
        )

        for recipient, branch_records in records_by_recipient.items():
            selected_handoff = _select_handoff(
                hop, recipient, placement.handoffs
            )
            if selected_handoff is None:
                parent_context = root_ctx
            else:
                selected_parent, handoff_log = selected_handoff
                parent_context = handoff_contexts.get(
                    id(handoff_log)
                ) or delivery_host_contexts.get(id(handoff_log))
                if parent_context is None:
                    parent_context = host_contexts[selected_parent][0]

            branch_logs = (
                [record.log for record in branch_records]
                if len(records_by_recipient) > 1
                else prepared.logs
            )
            host_recipients = (
                [recipient] if recipient is not None else all_host_recipients
            )
            host_next_queue_id = _join_unique(
                log.queued_as for log in branch_logs
            )
            host_relay_host = _join_unique(
                log.relay_host for log in branch_logs
            )
            host_relay_ip = _join_unique(log.relay_ip for log in branch_logs)
            host_relay_port = next(
                (
                    log.relay_port
                    for log in branch_logs
                    if log.relay_port is not None
                ),
                None,
            )
            host_next_host = next(
                (log.relay_host for log in branch_logs if log.relay_host),
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
                explicit_handoff=placement.explicit_handoff,
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

            for record in branch_records:
                delivery_host_contexts[id(record.log)] = host_ctx

            for record in branch_records:
                delay_start = record.start_time + hop_offsets[hop]
                delivery_span = create_delivery_span(
                    host,
                    delay_start,
                    host_ctx,
                    recipient=record.recipient,
                    transport=record.log.service,
                )
                if record.log.delivery_status in {
                    DeliveryStatus.TEMPORARY_FAILURE,
                    DeliveryStatus.PERMANENT_FAILURE,
                }:
                    _mark_failure_from_log(delivery_span, record.log)
                delivery_ctx = trace.set_span_in_context(delivery_span)
                delay_spans = create_delay_spans(
                    record.delays, host, delay_start, delivery_ctx
                )
                if delay_spans:
                    handoff_contexts[id(record.log)] = (
                        trace.set_span_in_context(delay_spans[-1])
                    )
                delivery_end = delay_start + timedelta(
                    seconds=get_effective_total_delay(record.delays)
                )
                delivery_span.end(end_time=dt_to_ns(delivery_end))

            logger.debug(
                "Close host span: %s recipient %s at %s",
                hop,
                recipient,
                host_end.isoformat(),
            )
            host_span.end(end_time=dt_to_ns(host_end))

    root_span.end(end_time=int(root_end.timestamp() * 1e9))


def export_traces(
    logs_by_message_id: dict[str, list[LogEntry]],
) -> int:
    """Build traces for the message-ID groups and return their count."""
    trace_count = 0

    for message_id, message_logs in logs_by_message_id.items():
        hops_logs = group_logs_by_hops(message_logs)
        logger.debug(
            "Processing email with message_id %s and hops %s",
            message_id,
            list(hops_logs),
        )
        prepared_hops = _prepare_hops(hops_logs)
        if not prepared_hops:
            logger.debug(
                "No delay info found for message_id %s, skipping", message_id
            )
            continue

        (
            hop_placements,
            hop_links,
            hop_parents,
            ordered_hops,
        ) = _plan_hops(message_id, prepared_hops)
        hop_offsets, hop_bounds = _calculate_hop_timing(
            ordered_hops,
            prepared_hops,
            hop_links,
            hop_parents,
        )
        trace_count += 1
        _emit_trace(
            message_id,
            message_logs,
            prepared_hops,
            hop_placements,
            ordered_hops,
            hop_offsets,
            hop_bounds,
        )

    return trace_count
