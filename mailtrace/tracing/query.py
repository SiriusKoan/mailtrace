"""Log querying and grouping functions for OpenSearch."""

import logging
import re
from datetime import datetime, timedelta
from typing import Dict

from opensearchpy import OpenSearch as OSClient
from opensearchpy.helpers.search import Search

from mailtrace.config import Config
from mailtrace.parser import LogEntry, OpensearchParser

logger = logging.getLogger("mailtrace")


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


def group_logs_by_message_id(
    logs: list[LogEntry],
) -> Dict[str, list[LogEntry]]:
    """Group log entries by message ID across all hops.

    One email maintains the same message-id throughout its delivery across
    multiple hosts, even though the queue_id changes at each hop.

    Returns a dictionary mapping message_id -> list of LogEntry containing all
    logs for that email across all hops.
    """
    grouped_logs: Dict[str, list[LogEntry]] = {}
    queue_id_to_msg_id_map: dict[tuple[str, str], str] = (
        {}
    )  # (hostname, queue ID) -> message ID

    for log in logs:
        message_id = _extract_message_id_from_log(log)
        if not message_id and not log.mail_id:
            continue
        if message_id:
            # If we have a message ID, use it directly
            if message_id not in grouped_logs:
                grouped_logs[message_id] = []
            grouped_logs[message_id].append(log)

            # If we also have a queue ID, map it to the message ID for future logs
            if log.mail_id:
                queue_id_to_msg_id_map[(log.hostname, log.mail_id)] = (
                    message_id
                )
        elif log.mail_id:
            # If we don't have a message ID but have a queue ID, try to find the message ID from previous logs
            key = (log.hostname, log.mail_id)
            if key in queue_id_to_msg_id_map:
                message_id = queue_id_to_msg_id_map[key]
                if message_id not in grouped_logs:
                    grouped_logs[message_id] = []
                grouped_logs[message_id].append(log)

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
