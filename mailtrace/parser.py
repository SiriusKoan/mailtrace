import re
from abc import ABC, abstractmethod
from typing import Any, Type

from mailtrace.config import OpenSearchMappingConfig
from mailtrace.models import LogEntry
from mailtrace.utils import analyze_log_from_message


def _get_nested_value(d: dict, key: str) -> Any:
    """
    Retrieve a value from a nested dictionary using a dot-separated key.
    """
    for k in key.split("."):
        if isinstance(d, dict):
            d = d.get(k, {})
        else:
            return None
    return d


def check_mail_id_valid(mail_id: str) -> bool:
    """
    Check if a mail ID is valid.

    Args:
        mail_id: The mail ID string to validate

    Returns:
        bool: True if the mail ID contains only alphanumeric characters (0-9, A-Z), False otherwise
    """

    return bool(re.match(r"^[0-9A-Z]+$", mail_id))


class LogParser(ABC):
    """Abstract base class for log parsers."""

    @abstractmethod
    def parse(self, log: Any) -> LogEntry:
        """
        Parse a log entry into a LogEntry object.

        Args:
            log: The log data to parse (format depends on concrete implementation)

        Returns:
            LogEntry: The parsed log entry
        """


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

        log_split = log.split(" ", 4)
        datetime = log_split[0]
        hostname = log_split[1]
        service = log_split[2].split("[")[0]
        mail_id = (
            log_split[3][:-1]
            if check_mail_id_valid(log_split[3][:-1])
            else None
        )
        message = log_split[4]

        # Extract relay information from message if available
        relay_host = None
        relay_ip = None
        relay_port = None
        smtp_code = None
        trace_result = analyze_log_from_message(message)
        if trace_result:
            relay_host = trace_result.relay_host
            relay_ip = trace_result.relay_ip
            relay_port = trace_result.relay_port
            smtp_code = trace_result.smtp_code

        return LogEntry(
            datetime,
            hostname,
            service,
            mail_id,
            message,
            relay_host,
            relay_ip,
            relay_port,
            smtp_code,
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

        log_split = log.split(" ", 6)
        datetime = " ".join(log_split[:3])
        hostname = log_split[3]
        service = log_split[4].split("[")[0]
        mail_id = (
            log_split[5][:-1]
            if check_mail_id_valid(log_split[5][:-1])
            else None
        )
        message = log_split[6]

        # Extract relay information from message if available
        relay_host = None
        relay_ip = None
        relay_port = None
        smtp_code = None
        trace_result = analyze_log_from_message(message)
        if trace_result:
            relay_host = trace_result.relay_host
            relay_ip = trace_result.relay_ip
            relay_port = trace_result.relay_port
            smtp_code = trace_result.smtp_code

        return LogEntry(
            datetime,
            hostname,
            service,
            mail_id,
            message,
            relay_host,
            relay_ip,
            relay_port,
            smtp_code,
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

    def parse(self, log: dict) -> LogEntry:
        """
        Parse a log entry from Opensearch/Elasticsearch format.

        Args:
            log: The log dictionary to parse

        Returns:
            LogEntry: The parsed log entry
        """

        datetime = _get_nested_value(log, self.mapping.timestamp)
        hostname = _get_nested_value(log, self.mapping.hostname)
        service = _get_nested_value(log, self.mapping.service)
        message_content = _get_nested_value(log, self.mapping.message)
        if not message_content:
            message_content = ""

        # Extract mail_id: use mapping if configured, otherwise parse from message
        if self.mapping.mail_id:
            mail_id = _get_nested_value(log, self.mapping.mail_id)
            message = message_content
        else:
            # Parse mail_id from message content
            _mail_id_candidate = message_content.split(":")[0]
            mail_id = (
                _mail_id_candidate
                if check_mail_id_valid(_mail_id_candidate)
                else None
            )
            message = (
                " ".join(message_content.split()[1:])
                if mail_id
                else message_content
            )

        # Extract relay information: try use mappings first
        relay_host = None
        relay_ip = None
        relay_port = None
        smtp_code = None
        if self.mapping.relay_host:
            relay_host = _get_nested_value(log, self.mapping.relay_host)
        if self.mapping.relay_ip:
            relay_ip = _get_nested_value(log, self.mapping.relay_ip)
        if self.mapping.relay_port:
            relay_port = _get_nested_value(log, self.mapping.relay_port)
        if self.mapping.smtp_code:
            smtp_code = _get_nested_value(log, self.mapping.smtp_code)

        # If any relay field is missing, try to fill from message parsing
        if not relay_host or not relay_ip or not relay_port or not smtp_code:
            trace_result = analyze_log_from_message(message_content)
            if trace_result:
                # Fill in missing fields from parsed message
                relay_host = relay_host or trace_result.relay_host
                relay_ip = relay_ip or trace_result.relay_ip
                relay_port = relay_port or trace_result.relay_port
                smtp_code = smtp_code or trace_result.smtp_code

        return LogEntry(
            datetime,
            hostname,
            service,
            mail_id,
            message,
            relay_host,
            relay_ip,
            relay_port,
            smtp_code,
        )


PARSERS: dict[str, Type[LogParser]] = {
    NoSpaceInDatetimeParser.__name__: NoSpaceInDatetimeParser,
    DayOfWeekParser.__name__: DayOfWeekParser,
    OpensearchParser.__name__: OpensearchParser,
}
