"""Log querying and grouping functions for OpenSearch."""

import logging
import re
from datetime import datetime, timedelta
from typing import Dict

from opensearchpy import OpenSearch as OSClient
from opensearchpy.helpers.search import Search

from mailtrace.config import Config
from mailtrace.parser import LogEntry, OpensearchParser
from mailtrace.tracing.models import EmailTrace

logger = logging.getLogger("mailtrace")


def query_logs_from_all_hosts(
    config: Config, start_time: str, end_time: str
) -> list[LogEntry]:
    """Query logs from OpenSearch index with time filtering.

    Fetches all logs from the configured index matching the time range,
    chains them directly without additional queries.

    Args:
        config: Configuration object
        start_time: Start time as ISO string (e.g., from datetime.utcnow())
        end_time: End time as ISO string (e.g., from datetime.utcnow())
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
        search = search.extra(size=10000)

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

        # Convert start and end times
        start_dt = datetime.fromisoformat(start_time)
        end_dt = datetime.fromisoformat(end_time)

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

        response = search.execute()

        # Parse and chain all logs directly
        parser = OpensearchParser(mapping=config.opensearch_config.mapping)
        all_logs = [
            parser.parse_with_enrichment(hit.to_dict()) for hit in response
        ]

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


def _trace_message_id_through_hops(logs: list[LogEntry]) -> Dict[str, str]:
    """Build a map from queue_id to message_id to track emails across hops.

    Returns:
        Dictionary mapping queue_id -> message_id
    """
    queue_id_to_msg_id: Dict[str, str] = {}

    # First pass: extract message_ids from logs that have them
    for log in logs:
        if log.mail_id:
            msg_id = _extract_message_id_from_log(log)
            if msg_id and log.mail_id not in queue_id_to_msg_id:
                queue_id_to_msg_id[log.mail_id] = msg_id
                logger.debug(
                    f"Found message ID {msg_id} for queue ID {log.mail_id} "
                    f"(from {log.hostname}/{log.service})"
                )

    # Second pass: trace through queued_as field to link queue_ids
    # If a log has queued_as, the next hop will use that as mail_id
    for log in logs:
        if (
            log.mail_id
            and log.queued_as
            and log.queued_as not in queue_id_to_msg_id
        ):
            # Propagate the message_id to the new queue_id
            if log.mail_id in queue_id_to_msg_id:
                msg_id = queue_id_to_msg_id[log.mail_id]
                queue_id_to_msg_id[log.queued_as] = msg_id
                logger.debug(
                    f"Propagated message ID {msg_id}: "
                    f"{log.mail_id} -> {log.queued_as} via queued_as "
                    f"(from {log.hostname}/{log.service})"
                )

    logger.debug(f"Queue ID to Message ID map: {queue_id_to_msg_id}")
    return queue_id_to_msg_id


def group_logs_by_message_id(logs: list[LogEntry]) -> Dict[str, EmailTrace]:
    """Group log entries by message ID across all hops.

    One email maintains the same message-id throughout its delivery across
    multiple hosts, even though the queue_id changes at each hop.

    Returns a dictionary mapping message_id -> EmailTrace containing all
    logs for that email across all hops.
    """
    traces: Dict[str, EmailTrace] = {}

    # Build mapping of queue_id -> message_id to trace emails across hops
    queue_id_to_msg_id = _trace_message_id_through_hops(logs)

    logs_without_identity = 0

    for log in logs:
        # Try to get message_id from the mapping
        message_id = None

        if log.mail_id and log.mail_id in queue_id_to_msg_id:
            message_id = queue_id_to_msg_id[log.mail_id]
        else:
            # If no mapping found, try to extract directly
            message_id = _extract_message_id_from_log(log)

        if not message_id:
            logs_without_identity += 1
            logger.debug(
                f"Log entry has no message-id: {log.hostname} {log.service} - {log.message[:100]}"
            )
            continue

        if message_id not in traces:
            traces[message_id] = EmailTrace(message_id)
            logger.debug(f"Created new trace for message ID: {message_id}")

        traces[message_id].add_entry(log)

    logger.info(
        f"Grouped {len(logs)} logs into {len(traces)} traces ({logs_without_identity} logs without message-id)"
    )
    return traces
