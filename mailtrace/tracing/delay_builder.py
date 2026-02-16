"""Delay builder for creating delay objects from email trace entries.

This module handles the logic of grouping log entries by hostname,
extracting delay information, and creating Delay objects with proper
time ranges.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from mailtrace.parser import LogEntry
from mailtrace.tracing.delay_parser import (
    DELAY_STAGES,
    DelayParser,
    parse_delay_info,
)
from mailtrace.tracing.models import Delay

logger = logging.getLogger("mailtrace")


class HostDelayExtractor:
    """Extracts delay information from log entries for a specific host."""

    def __init__(
        self,
        hostname: str,
        entries: list[LogEntry],
        parser: Optional[DelayParser] = None,
    ):
        """Initialize the extractor.

        Args:
            hostname: The hostname to extract delays for
            entries: Log entries for this host
            parser: Optional delay parser to use
        """
        self.hostname = hostname
        self.entries = entries
        self.parser = parser

    def extract_delays(self) -> dict[str, float]:
        """Extract delay values for each delay type from log entries.

        Returns:
            Dictionary mapping delay name to delay value in seconds
        """
        delays: dict[str, float] = {}

        # Find delay values from log entries
        for delay_name in DELAY_STAGES:
            for entry in self.entries:
                delay_info = parse_delay_info(entry.message, self.parser)
                delay_value = delay_info.get_delay(delay_name)

                if delay_value is not None:
                    delays[delay_name] = delay_value
                    break

        return delays

    def get_first_entry_time(self) -> datetime:
        """Get the timestamp of the first log entry for this host.

        Returns:
            The timestamp of the first entry
        """
        return datetime.fromisoformat(
            self.entries[0].datetime.replace("Z", "+00:00")
        )


class DelayBuilder:
    """Builds Delay objects from EmailTrace entries."""

    def __init__(self, parser: Optional[DelayParser] = None):
        """Initialize the builder.

        Args:
            parser: Optional delay parser to use for all entries
        """
        self.parser = parser

    def build_delays(
        self,
        message_id: str,
        entries: list[LogEntry],
    ) -> list[Delay]:
        """Build chronologically ordered delays from log entries.

        Groups log entries by hostname and creates Delay objects based on
        extracted delay information. Each host's delays are chained
        sequentially, starting from the first log entry's timestamp.

        Args:
            message_id: The message ID for logging purposes
            entries: List of log entries to process

        Returns:
            List of Delay objects representing each delay
        """
        if not entries:
            logger.debug(
                f"Message ID {message_id}: no entries to create delays"
            )
            return []

        # Sort entries chronologically
        sorted_entries = self._sort_entries_by_time(entries)

        logger.debug(
            f"Message ID {message_id}: processing {len(sorted_entries)} log entries"
        )

        # Group entries by hostname (preserving order)
        hostname_entries, hostname_order = self._group_entries_by_hostname(
            sorted_entries
        )

        # Extract delay values for each hostname
        hostname_delays = self._extract_hostname_delays(
            hostname_entries, hostname_order
        )

        # Create Delay objects by chaining delays for each host
        delays = self._create_delays_for_hosts(
            message_id,
            hostname_order,
            hostname_delays,
            hostname_entries,
        )

        logger.debug(f"Message ID {message_id}: created {len(delays)} delays")
        return delays

    def _sort_entries_by_time(self, entries: list[LogEntry]) -> list[LogEntry]:
        """Sort log entries by timestamp.

        Args:
            entries: List of log entries to sort

        Returns:
            Sorted list of log entries
        """
        return sorted(
            entries,
            key=lambda e: datetime.fromisoformat(
                e.datetime.replace("Z", "+00:00")
            ),
        )

    def _group_entries_by_hostname(
        self, entries: list[LogEntry]
    ) -> tuple[dict[str, list[LogEntry]], list[str]]:
        """Group log entries by hostname, preserving order of first appearance.

        Args:
            entries: List of log entries to group

        Returns:
            Tuple of (hostname->entries mapping, list of hostnames in order)
        """
        hostname_entries: dict[str, list[LogEntry]] = defaultdict(list)
        hostname_order: list[str] = []

        for entry in entries:
            if entry.hostname not in hostname_entries:
                hostname_order.append(entry.hostname)
            hostname_entries[entry.hostname].append(entry)

        return dict(hostname_entries), hostname_order

    def _extract_hostname_delays(
        self,
        hostname_entries: dict[str, list[LogEntry]],
        hostname_order: list[str],
    ) -> dict[str, dict[str, float]]:
        """Extract delay values for each hostname.

        Args:
            hostname_entries: Mapping of hostname to log entries
            hostname_order: List of hostnames in order

        Returns:
            Nested dictionary: hostname -> delay_name -> delay_value
        """
        hostname_delays: dict[str, dict[str, float]] = {}

        for hostname in hostname_order:
            extractor = HostDelayExtractor(
                hostname,
                hostname_entries[hostname],
                self.parser,
            )
            hostname_delays[hostname] = extractor.extract_delays()

        return hostname_delays

    def _create_delays_for_hosts(
        self,
        message_id: str,
        hostname_order: list[str],
        hostname_delays: dict[str, dict[str, float]],
        hostname_entries: dict[str, list[LogEntry]],
    ) -> list[Delay]:
        """Create Delay objects for all hosts by chaining delays sequentially.

        Args:
            message_id: Message ID for logging
            hostname_order: List of hostnames in order
            hostname_delays: Mapping of hostname to delays
            hostname_entries: Mapping of hostname to log entries

        Returns:
            List of Delay objects
        """
        delays: list[Delay] = []

        for hostname in hostname_order:
            host_delays = hostname_delays[hostname]
            entries = hostname_entries[hostname]

            # Each host starts at its first log entry's timestamp
            extractor = HostDelayExtractor(hostname, entries, self.parser)
            host_start_time = extractor.get_first_entry_time()
            current_time = host_start_time

            # Create Delay objects for this host in delay order
            for delay_name in DELAY_STAGES:
                if delay_name in host_delays:
                    delay_seconds = max(1e-6, host_delays[delay_name])
                    delay_start = current_time
                    delay_end = current_time + timedelta(seconds=delay_seconds)

                    logger.debug(
                        f"Message ID {message_id}: delay {hostname}/{delay_name} "
                        f"from {delay_start} to {delay_end} (duration {delay_seconds}s)"
                    )

                    delays.append(
                        Delay(
                            name=delay_name,
                            hostname=hostname,
                            start_time=delay_start,
                            end_time=delay_end,
                        )
                    )

                    # Move current_time forward for next delay
                    current_time = delay_end

        return delays
