"""Data models for email trace representation.

This module provides data classes for representing email traces and delays.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from mailtrace.parser import LogEntry

logger = logging.getLogger("mailtrace")


@dataclass
class Delay:
    """Represents a single delay in an email trace.

    Attributes:
        name: The delay name (e.g., 'before_qmgr', 'in_qmgr', 'conn_setup', 'transmission')
        hostname: The server hostname where this delay occurred
        start_time: When this delay began
        end_time: When this delay ended
    """

    name: str
    hostname: str
    start_time: datetime
    end_time: datetime


class EmailTrace:
    """Represents a complete email trace with multiple log entries."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        self.entries: list[LogEntry] = []
        self.queue_ids: set[str] = set()
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.sender: Optional[str] = None
        self.recipient: Optional[str] = None

    def add_entry(self, entry: LogEntry) -> None:
        """Add a log entry to this trace."""
        self.entries.append(entry)
        if entry.mail_id:
            self.queue_ids.add(entry.mail_id)

        # Update time boundaries
        entry_time = datetime.fromisoformat(
            entry.datetime.replace("Z", "+00:00")
        )
        if self.start_time is None or entry_time < self.start_time:
            self.start_time = entry_time
        if self.end_time is None or entry_time > self.end_time:
            self.end_time = entry_time

        # Extract sender/recipient from message using regex
        import re

        if not self.sender:
            from_match = re.search(r"from=<([^>]*)>", entry.message)
            if from_match:
                self.sender = from_match.group(1)
        if not self.recipient:
            to_match = re.search(r"to=<([^>]*)>", entry.message)
            if to_match:
                self.recipient = to_match.group(1)
