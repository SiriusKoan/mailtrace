import re

from ..log import logger
from ..models import LogQuery, PostfixServiceType
from .base import LogAggregator
from .opensearch import OpenSearch
from .ssh_host import SSHHost


def do_trace(mail_id: str, aggregator: LogAggregator) -> tuple[str, str]:
    """
    Trace a mail message through Postfix logs to find the next hop and mail ID.

    This function queries log entries for a given mail ID and analyzes SMTP/LMTP
    service entries to determine where the mail was relayed to next and what
    new mail ID it was assigned.

    Args:
        mail_id: The mail ID to trace through the logs
        aggregator: LogAggregator instance to query logs from

    Returns:
        A tuple containing (next_mail_id, next_hop) where:
        - next_mail_id: The new mail ID assigned when queued at next hop
        - next_hop: The domain name of the relay host the mail was sent to

    Example:
        >>> next_id, next_host = do_trace("ABC123", aggregator)
        >>> print(f"Mail relayed to {next_host} with ID {next_id}")
    """

    logger.info(f"Tracing mail ID: {mail_id}")
    logs = aggregator.query_by(LogQuery(mail_id=mail_id))
    next_hop: str = ""
    next_mail_id: str = ""
    for entry in logs:
        print(str(entry))

        if next_hop:
            # if next_hop is found, no logs should be further analyzed
            continue

        # Find the log messages with next hop information
        if entry.service in [
            PostfixServiceType.SMTP.value,
            PostfixServiceType.LMTP.value,
        ]:
            msg = entry.message
            match = re.search(r".*([0-9]{3})\s2\.0\.0.*", msg)
            if match:
                code = int(match.group(1))
                if code == 250:
                    mail_id_match = re.search(
                        r"250.*queued as ([0-9A-Z]+).*", msg
                    )
                    if mail_id_match:
                        next_mail_id = mail_id_match.group(1)
                        print(f"Queued as mail ID: {next_mail_id}")
                    relay_match = re.search(
                        r".*relay=([^\s]+)\[([^\]]+)\]:([0-9]+).*", msg
                    )
                    if relay_match:
                        domain = relay_match.group(1)
                        ip = relay_match.group(2)
                        port = relay_match.group(3)
                        print(f"Relay host: {domain}, {ip}, {port}")
                        next_hop = domain
    return next_mail_id, next_hop


__all__ = ["do_trace", "SSHHost", "OpenSearch"]
