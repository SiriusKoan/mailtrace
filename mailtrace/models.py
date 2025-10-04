"""Data models for the mailtrace application.

This module defines the core data structures used throughout the application,
including log entries, queries, and configuration models.
"""

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class LogEntry:
    """Represents a single log entry from a mail server log file.

    Attributes:
        datetime: Timestamp of the log entry
        hostname: Name of the host that generated the log entry
        service: Service that generated the log entry (e.g., postfix/smtp)
        mail_id: Unique identifier for the mail message, if available
        message: The actual log message content
    """

    # todo: datetime field should be converted to datetime object
    datetime: str
    hostname: str
    service: str
    mail_id: str | None
    message: str

    def __str__(self) -> str:
        return f"{self.datetime} {self.hostname} {self.service}: {self.mail_id}: {self.message}"


class PostfixServiceType(Enum):
    """Enumeration of common Postfix service types found in log files."""

    SMTP = "postfix/smtp"
    LMTP = "postfix/lmtp"
    SMTPD = "postfix/smtpd"
    QMGR = "postfix/qmgr"
    CLEANUP = "postfix/cleanup"


@dataclass
class LogQuery:
    """Query parameters for filtering log entries.

    Attributes:
        keywords: List of keywords to search for in log messages
        mail_id: Specific mail ID to filter by
        time: Specific timestamp to filter by
        time_range: Time range specification for filtering entries
    """

    keywords: list[str] = field(default_factory=list)
    mail_id: str | None = None
    time: str | None = None
    time_range: str | None = None
