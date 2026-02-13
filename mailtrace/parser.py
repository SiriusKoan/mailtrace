import re
from abc import ABC, abstractmethod
from typing import Any

from mailtrace.config import OpenSearchMappingConfig
from mailtrace.models import LogEntry
from mailtrace.utils import analyze_log_from_message

# Mail ID validation pattern (alphanumeric only)
_MAIL_ID_RE = re.compile(r"^[0-9A-Za-z]+$")

# Message-ID extraction from syslog message text (e.g., "message-id=<foo@bar>")
_MESSAGE_ID_RE = re.compile(r"message-id=<([^>]+)>")


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

    def parse_with_enrichment(self, log: Any) -> LogEntry:
        """
        Parse and enrich with relay info from message.

        Args:
            log: The log entry to parse

        Returns:
            LogEntry: The parsed and enriched log entry
        """
        entry = self.parse(log)
        return self._enrich_from_message(entry)

    @staticmethod
    def _enrich_from_message(entry: LogEntry) -> LogEntry:
        """
        Extract relay info and message_id from message if not already present.

        Args:
            entry: The LogEntry to enrich

        Returns:
            LogEntry: The enriched log entry
        """
        # Extract message_id from message text if not already set
        if entry.message_id is None:
            mid_match = _MESSAGE_ID_RE.search(entry.message)
            if mid_match:
                entry.message_id = mid_match.group(1)

        if all(
            [
                entry.relay_host,
                entry.relay_ip,
                entry.relay_port,
                entry.smtp_code,
                entry.queued_as,
            ]
        ):
            return entry  # Already complete

        result = analyze_log_from_message(entry.message)
        if result:
            if entry.queued_as is None:
                entry.queued_as = result.mail_id
            if entry.relay_host is None:
                entry.relay_host = result.relay_host
            if entry.relay_ip is None:
                entry.relay_ip = result.relay_ip
            if entry.relay_port is None:
                entry.relay_port = result.relay_port
            if entry.smtp_code is None:
                entry.smtp_code = result.smtp_code
        return entry


def _extract_mail_id(candidate: str) -> str | None:
    """Extract and validate mail ID from a candidate string (removes trailing colon)."""
    mail_id = candidate.rstrip(":")
    return mail_id if check_mail_id_valid(mail_id) else None


def _extract_service(service_with_pid: str) -> str:
    """Extract service name from a string like 'postfix/qmgr[123456]'."""
    return service_with_pid.split("[")[0]


class SyslogParser(LogParser):
    """
    Unified syslog parser that auto-detects and handles both RFC 3164 and RFC 5424 formats.

    Supported formats:
    - RFC 5424 (ISO 8601 timestamp):
      2025-01-01T10:00:00.123456+08:00 mailer1 postfix/qmgr[123456]: A2DE917F931: from=<abc@example.com>
    - RFC 3164 (BSD syslog):
      Feb 1 10:00:00 mailer1 postfix/qmgr[123456]: A2DE917F931: from=<abc@example.com>

    Format detection is based on the first character:
    - Digit (0-9): RFC 5424 format
    - Letter (a-zA-Z): RFC 3164 format
    """

    def parse(self, log: str) -> LogEntry:
        """
        Parse a syslog entry, auto-detecting the format.

        Args:
            log: The log string to parse

        Returns:
            LogEntry: The parsed log entry

        Raises:
            ValueError: If log format is invalid or unrecognized
        """
        if not log:
            raise ValueError("Empty log entry")

        # Auto-detect format based on first character
        if log[0].isdigit():
            return self._parse_rfc5424(log)
        if log[0].isalpha():
            return self._parse_rfc3164(log)
        raise ValueError(f"Unrecognized log format: {log}")

    @staticmethod
    def _parse_rfc5424(log: str) -> LogEntry:
        """
        Parse RFC 5424 format (ISO 8601 timestamp).

        Format: 2025-01-01T10:00:00.123456+08:00 hostname service[pid]: mail_id: message
        """
        # Split: [datetime, hostname, service_with_pid, mail_id_with_colon, ...message_parts]
        parts = log.split(" ", 4)
        if len(parts) < 4:
            raise ValueError(f"Invalid RFC 5424 log format: {log}")

        datetime_str = parts[0]
        hostname = parts[1]
        service = _extract_service(parts[2])  # Remove [PID]

        # parts[3] is "MAILID:" - message starts at parts[4]
        mail_id = _extract_mail_id(parts[3])
        message = parts[4] if len(parts) > 4 else ""

        return LogEntry(
            datetime=datetime_str,
            hostname=hostname,
            service=service,
            mail_id=mail_id,
            message=message,
        )

    @staticmethod
    def _parse_rfc3164(log: str) -> LogEntry:
        """
        Parse RFC 3164 format (BSD syslog with month abbreviation).

        Format: Feb 1 10:00:00 hostname service[pid]: mail_id: message
        Note: Handles double spaces for single-digit days (e.g., "Feb  1")
        """
        # Split all parts, then filter empty strings to handle double spaces
        parts = [p for p in log.split(" ") if p]

        if len(parts) < 6:
            raise ValueError(f"Invalid RFC 3164 log format: {log}")

        datetime_str = f"{parts[0]} {parts[1]} {parts[2]}"  # "Feb 1 10:00:00"
        hostname = parts[3]
        service = _extract_service(parts[4])
        mail_id = _extract_mail_id(parts[5])
        message = " ".join(parts[6:]) if len(parts) > 6 else ""

        return LogEntry(
            datetime=datetime_str,
            hostname=hostname,
            service=service,
            mail_id=mail_id,
            message=message,
        )


class Rfc5424Parser(SyslogParser):
    """
    Parser for RFC 5424 format only (ISO 8601 timestamp).

    Use this when you know all logs are in RFC 5424 format and want to skip auto-detection.
    Example: 2025-01-01T10:00:00.123456+08:00 mailer1 postfix/qmgr[123456]: A2DE917F931: ...
    """

    def parse(self, log: str) -> LogEntry:
        """Parse a log entry using RFC 5424 format."""
        return self._parse_rfc5424(log)


class Rfc3164Parser(SyslogParser):
    """
    Parser for RFC 3164 format only (BSD syslog with month abbreviation).

    Use this when you know all logs are in RFC 3164 format and want to skip auto-detection.
    Example: Feb 1 10:00:00 mailer1 postfix/qmgr[123456]: A2DE917F931: ...
    """

    def parse(self, log: str) -> LogEntry:
        """Parse a log entry using RFC 3164 format."""
        return self._parse_rfc3164(log)


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

    def _get_mapped_value(self, field: str, log: dict) -> Any:
        """Get value from mapping field if configured."""
        field_path = getattr(self.mapping, field, None)
        return _get_nested_value(log, field_path) if field_path else None

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

        # Strip mail_id prefix from message if present
        message = (
            " ".join(message_content.split()[1:])
            if mail_id and ":" in message_content
            else message_content
        )

        return LogEntry(
            datetime=_get_nested_value(log, self.mapping.timestamp),
            hostname=_get_nested_value(log, self.mapping.hostname),
            service=_get_nested_value(log, self.mapping.service),
            mail_id=mail_id,
            message=message,
            message_id=self._get_mapped_value("message_id", log),
            # Structured fields (may be None, will be enriched from message)
            queued_as=self._get_validated_mail_id(log, "queued_as"),
            relay_host=self._get_mapped_value("relay_host", log),
            relay_ip=self._get_mapped_value("relay_ip", log),
            relay_port=self._get_mapped_value("relay_port", log),
            smtp_code=self._get_mapped_value("smtp_code", log),
        )


# Registry of available parsers by name
PARSERS: dict[str, type[LogParser]] = {
    "SyslogParser": SyslogParser,  # Auto-detect format (default)
    "Rfc5424Parser": Rfc5424Parser,  # Force RFC 5424 (ISO 8601 timestamp)
    "Rfc3164Parser": Rfc3164Parser,  # Force RFC 3164 (BSD syslog)
    "OpensearchParser": OpensearchParser,
}
