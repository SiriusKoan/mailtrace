"""Log parsing utilities for the mailtrace application.

This module provides parsers for different mail server log formats,
converting raw log lines into structured LogEntry objects.
"""

import re
from abc import ABC, abstractmethod
from typing import Any, Type

from mailtrace.exceptions import LogParsingError
from mailtrace.models import LogEntry


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
        try:
            log_split = log.split(" ", 4)
            if len(log_split) < 5:
                raise LogParsingError(
                    f"Invalid log format: expected at least 5 fields, got {len(log_split)}",
                    "Check that your log_parser setting matches your actual log format",
                )
            datetime = log_split[0]
            hostname = log_split[1]
            service = log_split[2].split("[")[0]
            mail_id = (
                log_split[3][:-1]
                if check_mail_id_valid(log_split[3][:-1])
                else None
            )
            message = log_split[4]
            return LogEntry(datetime, hostname, service, mail_id, message)
        except IndexError as e:
            raise LogParsingError(
                f"Failed to parse log entry with NoSpaceInDatetimeParser: {e}",
                "Verify your log format matches: YYYY-MM-DDTHH:MM:SS hostname service[pid]: mail_id: message",
            ) from e


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
        try:
            log_split = log.split(" ", 6)
            if len(log_split) < 7:
                raise LogParsingError(
                    f"Invalid log format: expected at least 7 fields, got {len(log_split)}",
                    "Check that your log_parser setting matches your actual log format",
                )
            datetime = " ".join(log_split[:3])
            hostname = log_split[3]
            service = log_split[4].split("[")[0]
            mail_id = (
                log_split[5][:-1]
                if check_mail_id_valid(log_split[5][:-1])
                else None
            )
            message = log_split[6]
            return LogEntry(datetime, hostname, service, mail_id, message)
        except IndexError as e:
            raise LogParsingError(
                f"Failed to parse log entry with DayOfWeekParser: {e}",
                "Verify your log format matches: Mon DD HH:MM:SS hostname service[pid]: mail_id: message",
            ) from e


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

    def parse(self, log: dict) -> LogEntry:
        """
        Parse a log entry from Opensearch/Elasticsearch format.

        Args:
            log: The log dictionary to parse

        Returns:
            LogEntry: The parsed log entry
        """
        try:
            log = log["_source"]
            datetime = log["@timestamp"]
            hostname = log["log"]["syslog"]["hostname"]
            service = log["log"]["syslog"]["appname"]
            _mail_id_candidate = log["message"].split(":")[0]
            mail_id = (
                _mail_id_candidate
                if check_mail_id_valid(_mail_id_candidate)
                else None
            )
            message = (
                " ".join(log["message"].split()[1:])
                if check_mail_id_valid(_mail_id_candidate)
                else log["message"]
            )
            return LogEntry(datetime, hostname, service, mail_id, message)
        except KeyError as e:
            raise LogParsingError(
                f"Missing required field in OpenSearch log: {e}",
                "Verify your OpenSearch index contains the expected fields: @timestamp, log.syslog.hostname, log.syslog.appname, message",
            ) from e
        except Exception as e:
            raise LogParsingError(
                f"Failed to parse OpenSearch log entry: {e}",
                "Check that your OpenSearch document structure matches the expected format",
            ) from e


PARSERS: dict[str, Type[LogParser]] = {
    NoSpaceInDatetimeParser.__name__: NoSpaceInDatetimeParser,
    DayOfWeekParser.__name__: DayOfWeekParser,
    OpensearchParser.__name__: OpensearchParser,
}
