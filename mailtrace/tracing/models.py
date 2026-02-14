"""Data models for email trace representation."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from mailtrace.parser import LogEntry
from mailtrace.tracing.delay_parser import parse_delay_info

logger = logging.getLogger("mailtrace")

# Ordered delay stages for consistent stage ordering
DELAY_STAGES = [
    "delay_before_qmgr",
    "delay_in_qmgr",
    "delay_conn_setup",
    "delay_transmission",
]


@dataclass
class ServiceStage:
    """Represents a single service stage in an email trace.

    Attributes:
        hostname: The server hostname where this stage occurred
        delay_type: The type of delay (e.g., 'delay_before_qmgr', 'delay_in_qmgr')
        stage_name: Human-readable name for this stage
        start_time: When this stage began
        end_time: When this stage ended
        entries: List of log entries associated with this stage
    """

    hostname: str
    delay_type: str
    stage_name: str
    start_time: datetime
    end_time: datetime
    entries: list[LogEntry]


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

    def get_service_stages(self) -> list[ServiceStage]:
        """Get chronologically ordered service stages grouped by hostname and delay type.

        Groups log entries by hostname and creates stages based on delay types:
        - delay_before_qmgr: Time before queue manager
        - delay_in_qmgr: Time in queue manager
        - delay_conn_setup: Time for connection setup
        - delay_transmission: Time for transmission

        Each stage's duration is determined by the actual delay value from the logs.
        Within a single host, stages are chained sequentially.
        For cross-host transitions, each host starts at its first log entry's timestamp.

        Returns:
            List of ServiceStage objects representing each stage
        """
        if not self.entries:
            logger.debug(
                f"Message ID {self.message_id}: no entries to create stages"
            )
            return []

        # Sort entries by datetime
        sorted_entries = sorted(
            self.entries,
            key=lambda e: datetime.fromisoformat(
                e.datetime.replace("Z", "+00:00")
            ),
        )

        logger.debug(
            f"Message ID {self.message_id}: processing {len(sorted_entries)} log entries"
        )

        # Group entries by hostname
        hostname_entries: dict[str, list[LogEntry]] = {}
        hostname_order: list[str] = []

        for entry in sorted_entries:
            if entry.hostname not in hostname_entries:
                hostname_entries[entry.hostname] = []
                hostname_order.append(entry.hostname)
            hostname_entries[entry.hostname].append(entry)

        # Extract delay values for each hostname
        hostname_delays: dict[str, dict[str, float]] = {}

        for hostname in hostname_order:
            hostname_delays[hostname] = {}
            host_entries = hostname_entries[hostname]

            # Find delay values from log entries for this host
            for delay_type in DELAY_STAGES:
                for log_entry in host_entries:
                    delay_info = parse_delay_info(log_entry.message)
                    if delay_info[delay_type] is not None:
                        delay_value = delay_info[delay_type]
                        if delay_value is not None:  # Type guard
                            hostname_delays[hostname][delay_type] = delay_value
                        break

        # Create stages by chaining hosts together
        stages = []

        for hostname in hostname_order:
            delays = hostname_delays[hostname]
            host_entries = hostname_entries[hostname]

            # Each host starts at its first log entry's timestamp
            host_start_time = datetime.fromisoformat(
                host_entries[0].datetime.replace("Z", "+00:00")
            )
            current_time = host_start_time

            # Create stages for this host in delay order
            for delay_type in DELAY_STAGES:
                if delay_type in delays:
                    delay_seconds = delays[delay_type]
                    stage_start = current_time
                    stage_end = current_time + timedelta(seconds=delay_seconds)

                    logger.debug(
                        f"Message ID {self.message_id}: stage {hostname}.{delay_type} "
                        f"from {stage_start} to {stage_end} (duration {delay_seconds}s) "
                        f"with {len(host_entries)} entries"
                    )

                    stages.append(
                        ServiceStage(
                            hostname=hostname,
                            delay_type=delay_type,
                            stage_name=delay_type,
                            start_time=stage_start,
                            end_time=stage_end,
                            entries=host_entries,
                        )
                    )

                    # Move current_time forward for next stage
                    # This ensures proper alignment: next span starts where this one ends
                    current_time = stage_end

        logger.debug(
            f"Message ID {self.message_id}: created {len(stages)} stages"
        )
        return stages
