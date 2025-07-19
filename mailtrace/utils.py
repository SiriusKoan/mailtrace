import datetime
import re

from .aggregator import SSHHost
from .models import LogQuery, PostfixServiceType


def time_range_to_timedelta(time_range: str) -> datetime.timedelta:
    if time_range.endswith("d"):
        return datetime.timedelta(days=int(time_range[:-1]))
    if time_range.endswith("h"):
        return datetime.timedelta(hours=int(time_range[:-1]))
    if time_range.endswith("m"):
        return datetime.timedelta(minutes=int(time_range[:-1]))
    raise ValueError("Invalid time range")


def do_trace(mail_id: str, aggregator: SSHHost) -> tuple[str, str]:
    print(f"Tracing mail ID: {mail_id}")
    log = aggregator.query_by(LogQuery(mail_id=mail_id))
    next_hop: str = ""
    next_mail_id: str = ""
    for entry in log:
        if (entry.service == PostfixServiceType.SMTP.value) or (
            entry.service == PostfixServiceType.LMTP.value
        ):
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
