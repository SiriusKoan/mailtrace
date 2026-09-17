"""Log querying and grouping functions for OpenSearch."""

import logging
import re
from datetime import datetime, timedelta
from typing import Dict, MutableMapping

from opensearchpy import OpenSearch as OSClient
from opensearchpy.helpers.response import Hit
from opensearchpy.helpers.search import Search

from mailtrace.config import Config
from mailtrace.parser import LogEntry, OpensearchParser, extract_next_mail_id

logger = logging.getLogger("mailtrace")

HopKey = tuple[str, str]
QueueMessageMapping = MutableMapping[tuple[str, str], str]
SCROLL_KEEPALIVE = "2m"


def _normalize_hostname(hostname: str) -> str:
    """Return the short, case-insensitive hostname used for hop matching."""
    return hostname.rstrip(".").split(".", 1)[0].lower()


def query_all_logs(
    config: Config, start_time: datetime, end_time: datetime
) -> list[LogEntry]:
    """Query logs from OpenSearch index with time filtering.

    Fetches all logs from the configured index matching the time range,
    chains them directly without additional queries.

    Args:
        config: Configuration object
        start_time: Start time as datetime object
        end_time: End time as datetime object
    """
    try:
        # Create OpenSearch client from config
        client = OSClient(
            hosts=[
                {
                    "host": config.opensearch_config.host,
                    "port": config.opensearch_config.port,
                }
            ],
            http_auth=(
                (
                    config.opensearch_config.username,
                    config.opensearch_config.password,
                )
                if config.opensearch_config.username
                else None
            ),
            use_ssl=config.opensearch_config.use_ssl,
            verify_certs=config.opensearch_config.verify_certs,
            timeout=config.opensearch_config.timeout,
        )

        # Build single query targeting the configured index
        search = Search(using=client, index=config.opensearch_config.index)
        search = search.extra(size=config.tracing.scroll_batch_size)

        # Filter by facility (mail) if configured
        facility_field = config.opensearch_config.mapping.facility
        if facility_field:
            search = search.query("match", **{facility_field: "mail"})

        # Convert UTC time to configured timezone offset
        # e.g., if time is 13:00 UTC and timezone is +03:00, convert to 16:00
        tz_offset = config.opensearch_config.time_zone
        # Parse timezone offset (format: +HH:MM or -HH:MM)
        tz_sign = 1 if tz_offset[0] == "+" else -1
        tz_parts = tz_offset[1:].split(":")
        hours_offset = int(tz_parts[0])
        minutes_offset = int(tz_parts[1]) if len(tz_parts) > 1 else 0

        # Use provided datetime objects directly
        start_dt = start_time
        end_dt = end_time

        tz_delta = timedelta(
            hours=tz_sign * hours_offset, minutes=tz_sign * minutes_offset
        )
        start_dt_adjusted = start_dt + tz_delta
        end_dt_adjusted = end_dt + tz_delta

        start_time_adjusted = start_dt_adjusted.strftime("%Y-%m-%dT%H:%M:%S")
        end_time_adjusted = end_dt_adjusted.strftime("%Y-%m-%dT%H:%M:%S")

        # Filter by time range only
        search = search.filter(
            "range",
            **{
                config.opensearch_config.mapping.timestamp: {
                    "gte": start_time_adjusted,
                    "lt": end_time_adjusted,
                    "time_zone": config.opensearch_config.time_zone,
                }
            },
        )

        search = search.sort(
            {config.opensearch_config.mapping.timestamp: {"order": "asc"}}
        )

        logger.info(
            f"Querying {config.opensearch_config.index} index with time range "
            f"{start_time_adjusted} to {end_time_adjusted} "
            f"(timezone: {config.opensearch_config.time_zone})"
        )
        logger.debug(f"Query: {search.to_dict()}")

        parser = OpensearchParser(mapping=config.opensearch_config.mapping)
        all_logs: list[LogEntry] = []
        scroll_id: str | None = None

        try:
            response = client.search(
                index=config.opensearch_config.index,
                body=search.to_dict(),
                params={"scroll": SCROLL_KEEPALIVE},
            )

            while True:
                scroll_id = response.get("_scroll_id", scroll_id)
                hits = response.get("hits", {}).get("hits", [])
                if not hits:
                    break

                all_logs.extend(
                    parser.parse_with_enrichment(Hit(hit).to_dict())
                    for hit in hits
                )

                if not scroll_id:
                    logger.warning(
                        "OpenSearch returned hits without a scroll ID"
                    )
                    break

                response = client.scroll(
                    body={"scroll": SCROLL_KEEPALIVE, "scroll_id": scroll_id}
                )
        finally:
            if scroll_id:
                try:
                    client.clear_scroll(
                        body={"scroll_id": [scroll_id]},
                    )
                except Exception as clear_error:
                    logger.debug(
                        "Failed to clear OpenSearch scroll context: %s",
                        clear_error,
                    )

        logger.info(f"Found {len(all_logs)} log entries from index")

        # Debug: Log all entries to see what we're working with
        for i, log in enumerate(all_logs):
            logger.debug(
                f"Log {i}: {log.hostname} | {log.service} | mail_id={log.mail_id} | queued_as={log.queued_as} | {log.message}"
            )

        return all_logs

    except Exception as e:
        logger.error(f"Error querying logs from OpenSearch: {e}")
        return []


def _extract_message_id_from_log(log: LogEntry) -> str | None:
    """Extract message-id from log entry message content.

    Postfix logs contain message-id in the format: message-id=<id@domain>
    Exim logs contain message-id in the format: id=id@domain (without angle brackets)
    This is present in logs that include the message-id field.
    """
    # Try Postfix format first: message-id=<id@domain>
    msg_id_match = re.search(r"message-id=<([^>]+)>", log.message)
    if msg_id_match:
        return msg_id_match.group(1)

    # Try Exim format: id=id@domain (without angle brackets)
    exim_id_match = re.search(r"\bid=([\w\d.@-]+@[\w\d.-]+)", log.message)
    if exim_id_match:
        return exim_id_match.group(1)

    return None


def group_logs_by_message_id(
    logs: list[LogEntry],
    queue_id_to_msg_id_map: QueueMessageMapping | None = None,
) -> Dict[str, list[LogEntry]]:
    """Group log entries by message ID across all hops.

    One email maintains the same message-id throughout its delivery across
    multiple hosts, even though the queue_id changes at each hop.

    Returns a dictionary mapping message_id -> list of LogEntry containing all
    logs for that email across all hops.

    Args:
        logs: Log entries from one query batch.
        queue_id_to_msg_id_map: Optional mapping retained by the continuous
            tracer so queue-only entries can be resolved across query rounds.
    """
    grouped_logs: Dict[str, list[LogEntry]] = {}
    queue_mapping = (
        queue_id_to_msg_id_map if queue_id_to_msg_id_map is not None else {}
    )

    def register_queue_mappings(log: LogEntry, message_id: str) -> None:
        if log.mail_id:
            queue_mapping[(_normalize_hostname(log.hostname), log.mail_id)] = (
                message_id
            )

        if log.relay_host and log.queued_as:
            queue_mapping[
                (_normalize_hostname(log.relay_host), log.queued_as)
            ] = message_id

    resolved_message_ids: dict[int, str] = {}

    # Seed mappings from every explicit message-id before resolving queue-only logs.
    for index, log in enumerate(logs):
        message_id = _extract_message_id_from_log(log)
        if message_id:
            resolved_message_ids[index] = message_id
            register_queue_mappings(log, message_id)

    # Propagate mappings until no queue-only log can add another relay mapping.
    changed = True
    while changed:
        changed = False
        for index, log in enumerate(logs):
            if index in resolved_message_ids or not log.mail_id:
                continue
            message_id = queue_mapping.get(
                (_normalize_hostname(log.hostname), log.mail_id)
            )
            if message_id:
                resolved_message_ids[index] = message_id
                register_queue_mappings(log, message_id)
                changed = True

    for index, log in enumerate(logs):
        message_id = resolved_message_ids.get(index)
        if message_id:
            grouped_logs.setdefault(message_id, []).append(log)

    return grouped_logs


def group_logs_by_hosts(logs: list[LogEntry]) -> Dict[str, list[LogEntry]]:
    """Group log entries by hostname and service.

    Returns a dictionary mapping "hostname" -> list of LogEntry containing all
    logs for that host and service.
    """
    grouped_logs: Dict[str, list[LogEntry]] = {}
    for log in logs:
        if log.hostname not in grouped_logs:
            grouped_logs[log.hostname] = []
        grouped_logs[log.hostname].append(log)
    return grouped_logs


def group_logs_by_hops(logs: list[LogEntry]) -> dict[HopKey, list[LogEntry]]:
    """Group logs by host and queue ID so each queue lifetime is one hop."""
    grouped_logs: dict[HopKey, list[LogEntry]] = {}
    for log in logs:
        if not log.mail_id:
            continue
        key = (log.hostname, log.mail_id)
        grouped_logs.setdefault(key, []).append(log)
    return grouped_logs


def build_hop_handoffs(
    hops: dict[HopKey, list[LogEntry]],
) -> dict[HopKey, list[tuple[HopKey, LogEntry]]]:
    """Map downstream hops to every observed upstream handoff log."""
    hop_index = {
        (_normalize_hostname(hostname), queue_id): hop
        for hop in hops
        for hostname, queue_id in [hop]
    }
    handoffs: dict[HopKey, list[tuple[HopKey, LogEntry]]] = {}

    for source_hop, logs in hops.items():
        source_host, _ = source_hop
        for log in logs:
            next_queue_id = extract_next_mail_id(log)
            if not next_queue_id:
                continue
            next_host = log.relay_host or source_host
            target_hop = hop_index.get(
                (_normalize_hostname(next_host), next_queue_id)
            )
            if target_hop and target_hop != source_hop:
                handoffs.setdefault(target_hop, []).append((source_hop, log))

    return handoffs


def build_hop_links(
    hops: dict[HopKey, list[LogEntry]],
) -> dict[HopKey, tuple[HopKey, LogEntry]]:
    """Map each downstream hop to its first observed upstream handoff."""
    return {
        child_hop: handoffs[0]
        for child_hop, handoffs in build_hop_handoffs(hops).items()
    }


def build_hop_parents(
    hops: dict[HopKey, list[LogEntry]],
) -> dict[HopKey, HopKey]:
    """Map each observed downstream queue hop to its upstream queue hop."""
    return {
        child_hop: parent_hop
        for child_hop, (parent_hop, _) in build_hop_links(hops).items()
    }
