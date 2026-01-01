import re

from mailtrace.aggregator.base import TraceResult


def analyze_log_from_message(message: str) -> TraceResult | None:
    """
    Extract relay information from a log message.

    Analyzes a log message to extract SMTP code, next mail ID, relay hostname,
    relay IP address, and relay port number.

    Args:
        message: The log message to analyze.

    Returns:
        A TraceResult containing (smtp_code, next_mail_id, relay_host, relay_ip, relay_port)
        if relay information is found, None otherwise.

    Example:
        >>> msg = '250 2.0.0 OK: Message queued as ABC123 relay=mail.example.com[192.168.1.1]:25'
        >>> result = analyze_log_from_message(msg)
        >>> if result:
        ...     print(f"Mail ID: {result.mail_id}, Relay: {result.relay_host}[{result.relay_ip}]:{result.relay_port}")
    """
    _SUCCESS_RE = re.compile(r".*([0-9]{3})\s.*")
    _QUEUED_RE = re.compile(r"250.*queued as (?P<id>[0-9A-Z]+).*")
    _RELAY_RE = re.compile(
        r".*relay=(?P<host>[^\s]+)\[(?P<ip>[^\]]+)\]:(?P<port>[0-9]+).*"
    )

    success_match = _SUCCESS_RE.match(message)
    if not success_match:
        return None
    smtp_code = int(success_match.group(1))
    if smtp_code != 250:
        return None

    queued_match = _QUEUED_RE.search(message)
    if not queued_match:
        return None
    next_mail_id = queued_match.group("id")

    relay_match = _RELAY_RE.search(message)
    if not relay_match:
        return None
    relay_host = relay_match.group("host")
    relay_ip = relay_match.group("ip")
    relay_port = int(relay_match.group("port"))

    return TraceResult(
        mail_id=next_mail_id,
        smtp_code=smtp_code,
        relay_host=relay_host,
        relay_ip=relay_ip,
        relay_port=relay_port,
    )
