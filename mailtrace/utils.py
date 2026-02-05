import datetime
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("mailtrace")

# Regex patterns for time validation
_TIME_FORMAT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_TIME_RANGE_RE = re.compile(r"^(\d+)([dhm])$")

# IP address pattern (IPv4 or IPv6)
_IP_ADDRESS_RE = re.compile(
    r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$|^(?:[a-fA-F0-9]{1,4}:){7}[a-fA-F0-9]{1,4}$"
)


def time_validation(time: str, time_range: str) -> str:
    """
    Validate time and time_range parameters.

    Returns:
        Empty string if validation passes, error message if validation fails.
    """
    if time and not _TIME_FORMAT_RE.match(time):
        return f"Time {time} should be in format YYYY-MM-DD HH:MM:SS"
    if bool(time) != bool(time_range):
        return "Time and time-range must be provided together"
    if time_range and not _TIME_RANGE_RE.match(time_range):
        return "time_range should be in format [0-9]+[dhm]"
    return ""


def time_range_to_timedelta(time_range: str) -> datetime.timedelta:
    """
    Convert a time range string to a datetime.timedelta object.

    Supported formats: Nd (days), Nh (hours), Nm (minutes)

    Raises:
        ValueError: If time_range format is invalid
    """
    match = _TIME_RANGE_RE.match(time_range)
    if not match:
        raise ValueError(f"Invalid time range format: {time_range}")

    value = int(match.group(1))
    unit = match.group(2)

    if unit == "d":
        return datetime.timedelta(days=value)
    if unit == "h":
        return datetime.timedelta(hours=value)
    if unit == "m":
        return datetime.timedelta(minutes=value)

    raise ValueError(f"Invalid time range unit: {unit}")


def print_blue(text: str) -> None:
    """Print text in blue color using ANSI escape codes."""
    print(f"\033[94m{text}\033[0m")


def print_red(text: str) -> None:
    """Print text in red color using ANSI escape codes."""
    print(f"\033[91m{text}\033[0m")


def get_hosts(hostnames: list[str], domain: str) -> list[str]:
    """
    Generate a list of possible hostnames based on the given hostnames and domain.

    For each hostname:
    - IP addresses are kept as-is
    - FQDNs generate both the FQDN and short hostname
    - Short hostnames generate both the short name and FQDN with domain

    Args:
        hostnames: List of base hostnames to expand
        domain: The domain name to append to short hostnames

    Returns:
        Deduplicated list of all hostname variants.
    """
    logger.debug(f"Generating hosts for hostnames: {hostnames} and domain: {domain}")
    hosts: list[str] = []

    for hostname in hostnames:
        hostname = hostname.strip()
        if not hostname:
            continue

        # IP addresses are kept as-is
        if _IP_ADDRESS_RE.match(hostname):
            hosts.append(hostname)
            continue

        # Add both short and FQDN forms
        if "." in hostname:
            hosts.append(hostname)
            hosts.append(hostname.split(".")[0])
        else:
            hosts.append(hostname)
            if domain:
                hosts.append(f"{hostname}.{domain}")

    result = list(set(hosts))
    logger.debug(f"Generated hosts: {result}")
    return result


@dataclass
class RelayResult:
    """Result of analyzing a log entry to extract relay information."""

    mail_id: str | None
    relay_host: str | None
    relay_ip: str | None
    relay_port: int | None
    smtp_code: int | None


def analyze_log_from_message(message: str) -> RelayResult | None:
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

    return RelayResult(
        mail_id=next_mail_id,
        smtp_code=smtp_code,
        relay_host=relay_host,
        relay_ip=relay_ip,
        relay_port=relay_port,
    )
