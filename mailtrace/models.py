from dataclasses import dataclass, field
from enum import Enum


class EventType(str, Enum):
    """Normalized event categories extracted from mail logs."""

    UNKNOWN = "unknown"
    MESSAGE_ID = "message_id"
    CONNECTION = "connection"
    MILTER_REJECT = "milter_reject"
    QUEUE_ACTIVE = "queue_active"
    QUEUE_REMOVED = "queue_removed"
    SMTP_DELIVERY = "smtp_delivery"
    LMTP_DELIVERY = "lmtp_delivery"
    RSPAMD_SCAN = "rspamd_scan"


class DeliveryStatus(str, Enum):
    """Normalized delivery outcome for one log event."""

    UNKNOWN = "unknown"
    TEMPORARY_FAILURE = "temporary_failure"
    PERMANENT_FAILURE = "permanent_failure"
    FORWARDED = "forwarded"
    DELIVERED = "delivered"


class SmtpStatusClass(str, Enum):
    """SMTP status class, including enhanced status-code prefixes."""

    SUCCESS = "2xx"
    TEMPORARY_FAILURE = "4xx"
    PERMANENT_FAILURE = "5xx"


@dataclass
class LogEntry:
    """Represents a single log entry from a mail server log file.

    Attributes:
        datetime: Timestamp of the log entry
        hostname: Name of the host that generated the log entry
        service: Service that generated the log entry (e.g., postfix/smtp)
        mail_id: Unique identifier for the mail message, if available
        message: The actual log message content
        queued_as: The new mail ID when message was queued at next hop (OpenSearch structured field)
        relay_host: Hostname of the relay, if available
        relay_ip: IP address of the relay, if available
        relay_port: Port number of the relay connection, if available
        smtp_code: SMTP response code if relay information was extracted, if available
        delays: Dictionary of delay information, where keys are delay names and values are delay lengths in seconds.
                For postfix: contains 'before_qmgr', 'in_qmgr', 'conn_setup', 'transmission'
                For exim: contains 'receive_time', 'deliver_time', 'queue_time'
    """

    # todo: datetime field should be converted to datetime object
    datetime: str
    hostname: str
    service: str | None
    mail_id: str | None
    message: str
    queued_as: str | None = None
    relay_host: str | None = None
    relay_ip: str | None = None
    relay_port: int | None = None
    smtp_code: int | None = None
    delays: dict[str, float | None] = field(default_factory=dict)
    event_type: EventType = EventType.UNKNOWN
    delivery_status: DeliveryStatus = DeliveryStatus.UNKNOWN
    smtp_status_class: SmtpStatusClass | None = None
    is_terminal: bool | None = None
    smtp_enhanced_status_code: str | None = None
    mail_status: str | None = None

    def __str__(self) -> str:
        return f"{self.datetime} {self.hostname} {self.service}: {self.mail_id}: {self.message}"


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
