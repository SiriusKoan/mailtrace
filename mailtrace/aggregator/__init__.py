import logging
import re

from mailtrace.aggregator.base import LogAggregator
from mailtrace.aggregator.opensearch import OpenSearch
from mailtrace.aggregator.ssh_host import SSHHost
from mailtrace.config import Config, Method
from mailtrace.models import LogQuery
from mailtrace.parser import LogEntry
from mailtrace.utils import RelayResult

logger = logging.getLogger("mailtrace")

# Regex patterns for parsing Postfix log messages
_SMTP_CODE_RE = re.compile(r"([0-9]{3})\s")
_QUEUED_AS_RE = re.compile(r"250.*queued as (?P<id>[0-9A-Za-z]+)")
_RELAY_RE = re.compile(r"relay=(?P<host>[^\s]+)\[(?P<ip>[^\]]+)\]:(?P<port>[0-9]+)")
_MESSAGE_ID_RE = re.compile(r"message-id=<([^>]+)>")

# Services that perform mail relay (string constants)
_RELAY_SERVICES = {
    "postfix/smtp",
    "postfix/lmtp",
}


def extract_message_ids(logs: list[LogEntry]) -> set[str]:
    """Extract unique message_ids from a list of log entries.

    Checks both the structured message_id field and the message text.
    """
    message_ids: set[str] = set()
    for entry in logs:
        if entry.message_id:
            message_ids.add(entry.message_id)
        else:
            match = _MESSAGE_ID_RE.search(entry.message)
            if match:
                message_ids.add(match.group(1))
    return message_ids


def _extract_next_mail_id(log_entry: LogEntry) -> str | None:
    """Extract the next mail ID from a log entry (structured field or message)."""
    if log_entry.queued_as:
        return log_entry.queued_as

    queued_match = _QUEUED_AS_RE.search(log_entry.message)
    return queued_match.group("id") if queued_match else None


def _parse_relay_info(log_entry: LogEntry) -> RelayResult | None:
    """Parse relay information from a successful SMTP log entry."""
    smtp_match = _SMTP_CODE_RE.search(log_entry.message)
    if not smtp_match:
        return None

    smtp_code = int(smtp_match.group(1))
    if smtp_code != 250:
        return None

    next_mail_id = _extract_next_mail_id(log_entry)
    if not next_mail_id:
        return None

    relay_match = _RELAY_RE.search(log_entry.message)
    if not relay_match:
        return None

    return RelayResult(
        mail_id=next_mail_id,
        relay_host=relay_match.group("host"),
        relay_ip=relay_match.group("ip"),
        relay_port=int(relay_match.group("port")),
        smtp_code=smtp_code,
    )


def _find_relay_in_logs(log_entries: list[LogEntry]) -> RelayResult | None:
    """Find the first relay hop in a list of log entries.

    Iterates through log entries, filtering for relay services, and returns
    the first successfully parsed relay result.

    Args:
        log_entries: Log entries to search through.

    Returns:
        A RelayResult if a relay hop is found, None otherwise.
    """
    for log_entry in log_entries:
        logger.info("LogEntry: %s", log_entry)
        print(str(log_entry))
        if log_entry.service not in _RELAY_SERVICES:
            continue

        result = _parse_relay_info(log_entry)
        if result:
            logger.info(
                "Found relay %s [%s]:%d, new ID %s",
                result.relay_host,
                result.relay_ip,
                result.relay_port,
                result.mail_id,
            )
            return result

    return None


def do_trace_from_logs(mail_id: str, logs: list[LogEntry]) -> RelayResult | None:
    """
    Extract relay info from pre-fetched log entries without querying.

    Reuses the same relay-finding logic as do_trace(), but operates
    on logs that have already been fetched, avoiding redundant queries.

    Args:
        mail_id: The mail ID (used only for logging).
        logs: Pre-fetched log entries for this mail ID.

    Returns:
        A RelayResult if a relay hop is found, None otherwise.
    """
    logger.info("Tracing mail ID from cached logs: %s", mail_id)
    sorted_logs = sorted(logs, key=lambda e: e.datetime)
    result = _find_relay_in_logs(sorted_logs)
    if not result:
        logger.info("No next hop found for %s (from cache)", mail_id)
    return result


def do_trace(mail_id: str, aggregator: LogAggregator) -> RelayResult | None:
    """
    Trace a mail message through Postfix logs to find the next relay hop and new mail ID.

    This function queries log entries for a given mail ID and analyzes them to determine
    where the mail was relayed and captures the response details in a TraceResult.

    Args:
        mail_id: The original mail ID to trace through the logs.
        aggregator: LogAggregator instance to query logs from.

    Returns:
        A RelayResult object containing:
            mail_id: The new mail ID assigned when queued at the next hop
            relay_host: Hostname of the relay host
            relay_ip: IP address of the relay host
            relay_port: Port number used for relaying
            smtp_code: The SMTP response code (typically 250)

        None if no relay entry is found.

    Example:
        >>> result = do_trace("ABC123", aggregator)
        >>> if result:
        ...     print(f"Mail relayed to {result.relay_host} with ID {result.mail_id}")
    """
    logger.info("Tracing mail ID: %s", mail_id)
    log_entries = aggregator.query_by(LogQuery(mail_id=mail_id))
    result = _find_relay_in_logs(log_entries)
    if not result:
        logger.info("No next hop found for %s", mail_id)
    return result


def select_aggregator(config: Config) -> type[LogAggregator]:
    """
    Select and return the appropriate log aggregator class based on config method.

    Raises:
        ValueError: If the method is unsupported.
    """
    aggregators = {
        Method.SSH: SSHHost,
        Method.OPENSEARCH: OpenSearch,
    }

    aggregator_class = aggregators.get(config.method)
    if aggregator_class is None:
        raise ValueError(f"Unsupported method: {config.method}")
    return aggregator_class


__all__ = [
    "do_trace",
    "do_trace_from_logs",
    "extract_message_ids",
    "SSHHost",
    "OpenSearch",
    "RelayResult",
    "select_aggregator",
]
