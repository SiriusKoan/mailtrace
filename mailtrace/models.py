from dataclasses import dataclass, field


@dataclass
class LogEntry:
    """Represents a single log entry from a mail server log file.

    Attributes:
        datetime: Timestamp of the log entry
        hostname: Name of the host that generated the log entry
        service: Service that generated the log entry (e.g., postfix/smtp)
        mail_id: Unique identifier for the mail message, if available
        message: The actual log message content
        message_id: RFC 2822 Message-ID header value (without angle brackets), if available
        queued_as: The new mail ID when message was queued at next hop (OpenSearch structured field)
        relay_host: Hostname of the relay, if available
        relay_ip: IP address of the relay, if available
        relay_port: Port number of the relay connection, if available
        smtp_code: SMTP response code if relay information was extracted, if available
    """

    # todo: datetime field should be converted to datetime object
    datetime: str
    hostname: str
    service: str
    mail_id: str | None
    message: str
    message_id: str | None = None
    queued_as: str | None = None
    relay_host: str | None = None
    relay_ip: str | None = None
    relay_port: int | None = None
    smtp_code: int | None = None

    def __str__(self) -> str:
        return f"{self.datetime} {self.hostname} {self.service}: {self.mail_id}: {self.message}"


@dataclass
class LogQuery:
    """Query parameters for filtering log entries.

    Attributes:
        keywords: List of keywords to search for in log messages
        mail_id: Specific mail ID to filter by
        mail_ids: List of mail IDs for batch query
        message_id: RFC 2822 Message-ID to search across all hops
        time: Specific timestamp to filter by
        time_range: Time range specification for filtering entries
    """

    keywords: list[str] = field(default_factory=list)
    mail_id: str | None = None
    mail_ids: list[str] | None = None
    message_id: str | None = None
    time: str | None = None
    time_range: str | None = None
