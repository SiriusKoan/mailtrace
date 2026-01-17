import re
from abc import ABC, abstractmethod
from typing import Any

from mailtrace.config import OpenSearchMappingConfig
from mailtrace.models import LogEntry
from mailtrace.utils import analyze_log_from_message

# Mail ID validation pattern (alphanumeric only)
_MAIL_ID_RE = re.compile(r"^[0-9A-Z]+$")


def _get_nested_value(data: Any, key: str) -> Any:
    """Retrieve a value from a nested dictionary using a dot-separated key."""
    for k in key.split("."):
        if isinstance(data, dict):
            data = data.get(k)
        else:
            return None
    return data


def check_mail_id_valid(mail_id: str) -> bool:
    """
    Check if a mail ID is valid.

    Args:
        mail_id: The mail ID string to validate

    Returns:
        bool: True if the mail ID contains only alphanumeric characters (0-9, A-Z), False otherwise
    """
    return bool(_MAIL_ID_RE.match(mail_id))


class LogParser(ABC):
    """Abstract base class for log parsers."""

    @abstractmethod
    def parse(self, log: Any) -> LogEntry:
        """
        Parse a log entry into a LogEntry object.

        Args:
            log: The log entry to parse

        Returns:
            LogEntry: The parsed log entry
        """


def _extract_mail_id(candidate: str) -> str | None:
    """Extract and validate mail ID from a candidate string (removes trailing colon)."""
    mail_id = candidate.rstrip(":")
    return mail_id if check_mail_id_valid(mail_id) else None


def _extract_service(service_with_pid: str) -> str:
    """Extract service name from a string like 'postfix/qmgr[123456]'."""
    return service_with_pid.split("[")[0]


class NoSpaceInDatetimeParser(LogParser):
    """
    This parser is designed to handle log entries where the datetime does not contain any spaces.
    Example log format:
    2025-01-01T10:00:00.123456+08:00 mailer1 postfix/qmgr[123456]: A2DE917F931: from=<abc@example.com>, size=12345, nrcpt=1 (queue active)
    """

    def parse(self, log: str) -> LogEntry:
        """
        Parse a log entry with space-free datetime format.

        Args:
            log: The log string to parse

        Returns:
            LogEntry: The parsed log entry
        """
        parts = log.split(" ", 4)
        datetime = parts[0]
        hostname = parts[1]
        service = _extract_service(parts[2])
        mail_id = _extract_mail_id(parts[3])
        message = parts[4]

        # Extract relay information from message if available
        trace_result = analyze_log_from_message(message)

        return LogEntry(
            datetime=datetime,
            hostname=hostname,
            service=service,
            mail_id=mail_id,
            message=message,
            queued_as=trace_result.mail_id if trace_result else None,
            relay_host=trace_result.relay_host if trace_result else None,
            relay_ip=trace_result.relay_ip if trace_result else None,
            relay_port=trace_result.relay_port if trace_result else None,
            smtp_code=trace_result.smtp_code if trace_result else None,
        )


class DayOfWeekParser(LogParser):
    """
    This parser is designed to handle log entries where the datetime includes the day of the week.
    Example log format:
    Feb 1 10:00:00 mailer1 postfix/qmgr[123456]: A2DE917F931: from=<abc@example.com>, size=12345, nrcpt=1 (queue active)
    """

    def parse(self, log: str) -> LogEntry:
        """
        Parse a log entry with day-of-week datetime format.

        Args:
            log: The log string to parse

        Returns:
            LogEntry: The parsed log entry
        """
        parts = log.split(" ", 6)
        datetime = " ".join(parts[:3])
        hostname = parts[3]
        service = _extract_service(parts[4])
        mail_id = _extract_mail_id(parts[5])
        message = parts[6]

        # Extract relay information from message if available
        trace_result = analyze_log_from_message(message)

        return LogEntry(
            datetime=datetime,
            hostname=hostname,
            service=service,
            mail_id=mail_id,
            message=message,
            queued_as=trace_result.mail_id if trace_result else None,
            relay_host=trace_result.relay_host if trace_result else None,
            relay_ip=trace_result.relay_ip if trace_result else None,
            relay_port=trace_result.relay_port if trace_result else None,
            smtp_code=trace_result.smtp_code if trace_result else None,
        )


class OpensearchParser(LogParser):
    """
    This parser is designed to handle log entries from Opensearch/Elasticsearch format.
    Example log format (dict structure):
    {
        "_source": {
            "@timestamp": "2025-01-01T10:00:00.123Z",
            "log": {
                "syslog": {
                    "hostname": "mailer1.example.com",
                    "appname": "postfix/qmgr"
                }
            },
            "message": "A2DE917F931: from=<abc@example.com>, size=12345, nrcpt=1 (queue active)"
        }
    }
    """

    def __init__(self, mapping: OpenSearchMappingConfig):
        self.mapping = mapping

    def _get_validated_mail_id(self, log: dict, field_name: str) -> str | None:
        """Get a mail ID from a mapping field, returning None if invalid."""
        field_path = getattr(self.mapping, field_name, None)
        if not field_path:
            return None
        value = _get_nested_value(log, field_path)
        if value and check_mail_id_valid(value):
            return value
        return None

    def _extract_mail_id(self, log: dict, message_content: str) -> str | None:
        """Extract mail ID from structured field or message content."""
        # Try structured field first (queueid)
        mail_id = self._get_validated_mail_id(log, "queueid")
        if mail_id:
            return mail_id

        # Try mail_id mapping if configured
        if self.mapping.mail_id:
            mail_id = _get_nested_value(log, self.mapping.mail_id)
            if mail_id:
                return mail_id

        # Parse mail_id from message content
        mail_id_candidate = message_content.split(":")[0]
        return (
            mail_id_candidate
            if check_mail_id_valid(mail_id_candidate)
            else None
        )

    def _extract_relay_info(
        self, log: dict, message_content: str
    ) -> tuple[str | None, str | None, int | None, int | None]:
        """Extract relay information from structured fields or message content."""
        # Try structured fields first
        relay_host = (
            _get_nested_value(log, self.mapping.relay_host)
            if self.mapping.relay_host
            else None
        )
        relay_ip = (
            _get_nested_value(log, self.mapping.relay_ip)
            if self.mapping.relay_ip
            else None
        )
        relay_port = (
            _get_nested_value(log, self.mapping.relay_port)
            if self.mapping.relay_port
            else None
        )
        smtp_code = (
            _get_nested_value(log, self.mapping.smtp_code)
            if self.mapping.smtp_code
            else None
        )

        # If any field is missing, try parsing from message
        if not all([relay_host, relay_ip, relay_port, smtp_code]):
            trace_result = analyze_log_from_message(message_content)
            if trace_result:
                relay_host = relay_host or trace_result.relay_host
                relay_ip = relay_ip or trace_result.relay_ip
                relay_port = relay_port or trace_result.relay_port
                smtp_code = smtp_code or trace_result.smtp_code

        return relay_host, relay_ip, relay_port, smtp_code

    def parse(self, log: dict) -> LogEntry:
        """
        Parse a log entry from OpenSearch/Elasticsearch format.

        Args:
            log: The log dictionary to parse

        Returns:
            LogEntry: The parsed log entry
        """
        message_content = _get_nested_value(log, self.mapping.message) or ""
        mail_id = self._extract_mail_id(log, message_content)
        queued_as = self._get_validated_mail_id(log, "queued_as")

        # Strip mail_id prefix from message if present
        message = (
            " ".join(message_content.split()[1:])
            if mail_id and ":" in message_content
            else message_content
        )

        relay_host, relay_ip, relay_port, smtp_code = self._extract_relay_info(
            log, message_content
        )

        return LogEntry(
            datetime=_get_nested_value(log, self.mapping.timestamp),
            hostname=_get_nested_value(log, self.mapping.hostname),
            service=_get_nested_value(log, self.mapping.service),
            mail_id=mail_id,
            message=message,
            queued_as=queued_as,
            relay_host=relay_host,
            relay_ip=relay_ip,
            relay_port=relay_port,
            smtp_code=smtp_code,
        )


# Registry of available parsers by class name
PARSERS: dict[str, type[LogParser]] = {
    NoSpaceInDatetimeParser.__name__: NoSpaceInDatetimeParser,
    DayOfWeekParser.__name__: DayOfWeekParser,
    OpensearchParser.__name__: OpensearchParser,
}
