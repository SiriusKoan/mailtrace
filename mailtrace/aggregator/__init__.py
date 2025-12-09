import re
from dataclasses import dataclass

from mailtrace.aggregator.base import LogAggregator
from mailtrace.aggregator.opensearch import OpenSearch
from mailtrace.aggregator.ssh_host import SSHHost
from mailtrace.log import logger
from mailtrace.models import LogQuery, PostfixServiceType

_SUCCESS_RE = re.compile(r".*([0-9]{3})\s.*")
_QUEUED_RE = re.compile(r"250.*queued as (?P<id>[0-9A-Z]+).*")
_RELAY_RE = re.compile(r".*relay=(?P<host>[^\s]+)\[(?P<ip>[^\]]+)\]:(?P<port>[0-9]+).*")


@dataclass
class TraceResult:
    mail_id: str
    relay_host: str
    relay_ip: str
    relay_port: int
    smtp_code: int


def do_trace(mail_id: str, aggregator: LogAggregator) -> TraceResult | None:
    """
    Trace a mail message through Postfix logs to find the next relay hop and new mail ID.

    This function queries log entries for a given mail ID and analyzes SMTP/LMTP
    service entries to determine where the mail was relayed and captures the
    response details in a TraceResult.

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
    for log_entry in log_entries:
        logger.debug("LogEntry: %s", log_entry)
        if log_entry.service not in {
            PostfixServiceType.SMTP.value,
            PostfixServiceType.LMTP.value,
        }:
            continue

        success_match = _SUCCESS_RE.match(log_entry.message)
        if not success_match:
            continue
        smtp_code = int(success_match.group(1))
        if smtp_code != 250:
            continue

        queued_match = _QUEUED_RE.search(log_entry.message)
        if not queued_match:
            continue
        next_mail_id = queued_match.group("id")

        relay_match = _RELAY_RE.search(log_entry.message)
        if not relay_match:
            continue
        relay_host = relay_match.group("host")
        relay_ip = relay_match.group("ip")
        relay_port = int(relay_match.group("port"))

        logger.info(
            "Found relay %s [%s]:%d, new ID %s",
            relay_host,
            relay_ip,
            relay_port,
            next_mail_id,
        )
        return TraceResult(
            mail_id=next_mail_id,
            relay_host=relay_host,
            relay_ip=relay_ip,
            relay_port=relay_port,
            smtp_code=smtp_code,
        )

    logger.info("No next hop found for %s", mail_id)
    return None


__all__ = ["do_trace", "SSHHost", "OpenSearch", "TraceResult"]
