"""Event metadata extracted from mail log messages."""

import re

from mailtrace.models import (
    DeliveryStatus,
    EventType,
    LogEntry,
    SmtpStatusClass,
)

_ENHANCED_STATUS_RE = re.compile(r"(?<!\w)([245])\.\d+\.\d+(?!\w)")
_SMTP_REPLY_RE = re.compile(r"(?<![\d.])([245]\d{2})(?=\s)")
_NEXT_QUEUE_RE = re.compile(
    r"(?:queued as|forwarded as)\s+([A-Za-z0-9_-]+)|\bid=([A-Za-z0-9_-]+)"
)


def _status_class_from_prefix(prefix: str) -> SmtpStatusClass:
    if prefix == "2":
        return SmtpStatusClass.SUCCESS
    if prefix == "4":
        return SmtpStatusClass.TEMPORARY_FAILURE
    if prefix == "5":
        return SmtpStatusClass.PERMANENT_FAILURE
    raise ValueError(f"Unsupported SMTP status class: {prefix}")


def extract_smtp_status_class(
    message: str, smtp_code: int | str | None = None
) -> SmtpStatusClass | None:
    """Extract an SMTP class from reply or enhanced status codes."""
    enhanced_match = _ENHANCED_STATUS_RE.search(message)
    if enhanced_match:
        return _status_class_from_prefix(enhanced_match.group(1))

    reply_match = _SMTP_REPLY_RE.search(message)
    if reply_match:
        return _status_class_from_prefix(reply_match.group(1)[0])

    normalized_code: int | None = None
    if isinstance(smtp_code, int):
        normalized_code = smtp_code
    elif isinstance(smtp_code, str) and smtp_code.isdigit():
        normalized_code = int(smtp_code)
    if normalized_code is not None and 200 <= normalized_code <= 599:
        prefix = str(normalized_code)[0]
        if prefix in {"2", "4", "5"}:
            return _status_class_from_prefix(prefix)
    return None


def _event_type(entry: LogEntry) -> EventType:
    message = (entry.message or "").lower()
    service = (entry.service or "").lower()

    if "milter-reject" in message:
        return EventType.MILTER_REJECT
    if "rspamd" in service or "rspamd_" in message:
        return EventType.RSPAMD_SCAN
    if service == "postfix/lmtp":
        return EventType.LMTP_DELIVERY
    if service == "postfix/smtp":
        return EventType.SMTP_DELIVERY
    if "queue active" in message:
        return EventType.QUEUE_ACTIVE
    if re.search(r"\bremoved\b", message):
        return EventType.QUEUE_REMOVED
    if "message-id=<" in message:
        return EventType.MESSAGE_ID
    if "client=" in message or "connect from" in message:
        return EventType.CONNECTION
    return EventType.UNKNOWN


def _has_next_queue(entry: LogEntry) -> bool:
    if entry.queued_as:
        return True
    return _NEXT_QUEUE_RE.search(entry.message or "") is not None


def _delivery_status(
    entry: LogEntry, status_class: SmtpStatusClass | None
) -> DeliveryStatus:
    message = (entry.message or "").lower()

    if entry.event_type is EventType.MILTER_REJECT:
        if status_class is SmtpStatusClass.TEMPORARY_FAILURE:
            return DeliveryStatus.TEMPORARY_FAILURE
        if status_class is SmtpStatusClass.PERMANENT_FAILURE:
            return DeliveryStatus.PERMANENT_FAILURE
        if any(term in message for term in ("try again later", "greylist")):
            return DeliveryStatus.TEMPORARY_FAILURE

    if re.search(r"\b(?:deferred|defer|greylist|soft reject)\b", message):
        return DeliveryStatus.TEMPORARY_FAILURE
    if re.search(
        r"\b(?:bounced|undeliverable|permanent failure)\b", message
    ):
        return DeliveryStatus.PERMANENT_FAILURE

    if re.search(r"\bstatus=sent\b", message):
        return (
            DeliveryStatus.FORWARDED
            if _has_next_queue(entry)
            else DeliveryStatus.DELIVERED
        )
    if " saved" in f" {message}" or message.endswith("saved"):
        return DeliveryStatus.DELIVERED
    return DeliveryStatus.UNKNOWN


def classify_log_entry(entry: LogEntry) -> LogEntry:
    """Attach normalized event and lifecycle metadata to a parsed log entry."""
    entry.event_type = _event_type(entry)
    entry.smtp_status_class = extract_smtp_status_class(
        entry.message or "", entry.smtp_code
    )
    entry.delivery_status = _delivery_status(
        entry, entry.smtp_status_class
    )
    if entry.delivery_status in {
        DeliveryStatus.DELIVERED,
        DeliveryStatus.PERMANENT_FAILURE,
    }:
        entry.is_terminal = True
    elif entry.delivery_status in {
        DeliveryStatus.FORWARDED,
        DeliveryStatus.TEMPORARY_FAILURE,
    }:
        entry.is_terminal = False
    else:
        entry.is_terminal = None
    return entry
