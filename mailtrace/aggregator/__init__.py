import re

from ..log import logger
from ..models import LogQuery, PostfixServiceType
from .base import LogAggregator
from .opensearch import Opensearch
from .ssh_host import SSHHost


def do_trace(mail_id: str, aggregator: LogAggregator) -> tuple[str, str]:
    logger.info(f"Tracing mail ID: {mail_id}")
    logs = aggregator.query_by(LogQuery(mail_id=mail_id))
    next_hop: str = ""
    next_mail_id: str = ""
    for entry in logs:
        print(str(entry))
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


__all__ = ["do_trace", "SSHHost", "Opensearch"]
