import logging

from mailtrace.aggregator.base import LogAggregator
from mailtrace.aggregator.opensearch import OpenSearch
from mailtrace.aggregator.ssh_host import SSHHost
from mailtrace.config import Config, Method
from mailtrace.models import LogQuery
from mailtrace.parser import (
    parse_exim_relay_info,
    parse_postfix_relay_info,
)
from mailtrace.utils import RelayResult, print_blue

logger = logging.getLogger("mailtrace")

# Services that perform mail relay (string constants)
_RELAY_SERVICES = {
    "postfix/smtp",
    "postfix/lmtp",
    "exim",
    "exim4",
}


def do_trace(mail_id: str, aggregator: LogAggregator) -> RelayResult | None:
    """
    Trace a mail message through Postfix logs to find the next relay hop and new mail ID.

    This function queries log entries for a given mail ID and analyzes them to determine
    where the mail was relayed and captures the response details in a TraceResult.
    All logs are printed before analysis is performed, and the function returns after
    all logs have been examined.

    Args:
        mail_id: The original mail ID to trace through the logs.
        aggregator: LogAggregator instance to query logs from.

    Returns:
        A TraceResult object containing:
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

    # Print all log entries
    print_blue("=== Log Entries ===")
    for log_entry in log_entries:
        print(log_entry)
    print_blue("===================")

    # Analyze log entries to find relay information
    for log_entry in log_entries:
        if log_entry.service not in _RELAY_SERVICES:
            continue

        # Try Postfix relay parsing first
        if log_entry.service in ("exim", "exim4"):
            result = parse_exim_relay_info(log_entry)
        else:
            result = parse_postfix_relay_info(log_entry)

        if result:
            logger.info(
                "Found relay %s [%s]:%d, new ID %s",
                result.relay_host,
                result.relay_ip,
                result.relay_port,
                result.mail_id,
            )
            return result

    logger.info("No next hop found for %s", mail_id)
    return None


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
    "SSHHost",
    "OpenSearch",
    "RelayResult",
    "select_aggregator",
]
