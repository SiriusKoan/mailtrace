"""Delay information parsers for email trace entries.

This module provides an extensible interface for parsing delay information
from email log messages. Different mail systems can have different delay
formats, so parsers can be added for each system.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

# Ordered delay stages for consistent stage ordering
# Postfix delay stages
POSTFIX_DELAY_STAGES = [
    "before_qmgr",
    "in_qmgr",
    "conn_setup",
    "transmission",
]

# Exim delay stages (in chronological order: receive -> queue -> deliver)
EXIM_DELAY_STAGES = [
    "receive_time",
    "queue_time",
    "deliver_time",
]

# Default delay stages (for backward compatibility)
DELAY_STAGES = POSTFIX_DELAY_STAGES


@dataclass
class DelayInfo:
    """Parsed delay information from a log message.

    Attributes:
        total_delay: Total message delay in seconds
        before_qmgr: Delay before queue manager (Postfix)
        in_qmgr: Delay in queue manager (Postfix)
        conn_setup: Delay in connection setup (Postfix)
        transmission: Delay in transmission (Postfix)
        queue_time: Queue time (Exim)
        receive_time: Receive time (Exim)
        deliver_time: Delivery time (Exim)
    """

    total_delay: Optional[float] = None
    # Postfix delays
    before_qmgr: Optional[float] = None
    in_qmgr: Optional[float] = None
    conn_setup: Optional[float] = None
    transmission: Optional[float] = None
    # Exim delays
    queue_time: Optional[float] = None
    receive_time: Optional[float] = None
    deliver_time: Optional[float] = None

    def get_delay(self, stage_name: str) -> Optional[float]:
        """Get delay value for a specific stage name.

        Args:
            stage_name: The stage name (e.g., 'before_qmgr', 'queue_time')

        Returns:
            The delay value in seconds, or None if not available
        """
        return getattr(self, stage_name, None)

    def has_breakdown(self) -> bool:
        """Check if this delay info has a breakdown of delays."""
        return any(
            [
                self.before_qmgr is not None,
                self.in_qmgr is not None,
                self.conn_setup is not None,
                self.transmission is not None,
                self.queue_time is not None,
                self.receive_time is not None,
                self.deliver_time is not None,
            ]
        )


class DelayParser(ABC):
    """Abstract base class for delay parsers."""

    @abstractmethod
    def parse(self, message: str) -> DelayInfo:
        """Parse delay information from a log message.

        Args:
            message: The log message to parse

        Returns:
            DelayInfo object with parsed delay information
        """
        pass

    @abstractmethod
    def get_mta_type(self) -> str:
        """Get the MTA type for this parser.

        Returns:
            MTA type string (e.g., 'postfix', 'exim')
        """
        pass

    @abstractmethod
    def get_delay_stages(self) -> list[str]:
        """Get the delay stages for this parser.

        Returns:
            List of delay stage names
        """
        pass


class PostfixDelayParser(DelayParser):
    """Parser for Postfix-style delay information.

    Postfix logs contain delays in the format:
    - delay=X.XX (total delay in seconds)
    - delays=A/B/C/D (breakdown: before_qmgr/in_qmgr/conn_setup/transmission)
    """

    def parse(self, message: str) -> DelayInfo:
        """Parse delay information from a Postfix log message.

        Args:
            message: The Postfix log message to parse

        Returns:
            DelayInfo object with parsed delay information
        """
        delay_info = DelayInfo()

        # Parse total delay: delay=X.XX
        total_match = re.search(r"delay=([\d.]+)", message)
        if total_match:
            delay_info.total_delay = float(total_match.group(1))

        # Parse delays breakdown: delays=A/B/C/D
        breakdown_match = re.search(
            r"delays=([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", message
        )
        if breakdown_match:
            delay_info.before_qmgr = float(breakdown_match.group(1))
            delay_info.in_qmgr = float(breakdown_match.group(2))
            delay_info.conn_setup = float(breakdown_match.group(3))
            delay_info.transmission = float(breakdown_match.group(4))

        return delay_info

    def get_mta_type(self) -> str:
        """Get the MTA type for this parser."""
        return "postfix"

    def get_delay_stages(self) -> list[str]:
        """Get the delay stages for this parser."""
        return POSTFIX_DELAY_STAGES


class EximDelayParser(DelayParser):
    """Parser for Exim-style delay information.

    Exim logs contain delays in the format:
    - QT=X.XXs (queue time - total time in queue)
    - RT=X.XXs (receive time - time to receive message)
    - DT=X.XXs (delivery time - time to deliver message)

    The actual queue waiting time is calculated as: QT - RT - DT
    """

    def parse(self, message: str) -> DelayInfo:
        """Parse delay information from an Exim log message.

        Args:
            message: The Exim log message to parse

        Returns:
            DelayInfo object with parsed delay information
        """
        delay_info = DelayInfo()

        # Parse QT (Queue Time): QT=X.XXs
        qt_match = re.search(r"QT=([\d.]+)s?", message)
        if qt_match:
            qt = float(qt_match.group(1))
            delay_info.total_delay = qt

        # Parse RT (Receive Time): RT=X.XXs
        rt_match = re.search(r"RT=([\d.]+)s?", message)
        if rt_match:
            delay_info.receive_time = float(rt_match.group(1))

        # Parse DT (Delivery Time): DT=X.XXs
        dt_match = re.search(r"DT=([\d.]+)s?", message)
        if dt_match:
            delay_info.deliver_time = float(dt_match.group(1))

        # Calculate queue_time = QT - RT - DT
        if delay_info.total_delay is not None:
            rt = delay_info.receive_time or 0.0
            dt = delay_info.deliver_time or 0.0
            delay_info.queue_time = max(0.0, delay_info.total_delay - rt - dt)

        print(
            f"Parsed Exim delay info: total_delay={delay_info.total_delay}, receive_time={delay_info.receive_time}, deliver_time={delay_info.deliver_time}, queue_time={delay_info.queue_time}"
        )

        return delay_info

    def get_mta_type(self) -> str:
        """Get the MTA type for this parser."""
        return "exim"

    def get_delay_stages(self) -> list[str]:
        """Get the delay stages for this parser."""
        return EXIM_DELAY_STAGES


# Default parser instance
_default_parser = PostfixDelayParser()


def parse_delay_info(
    message: str, parser: Optional[DelayParser] = None
) -> DelayInfo:
    """Parse delay information from a log message using the specified parser.

    Args:
        message: The log message to parse
        parser: Optional custom parser. If not provided, uses PostfixDelayParser

    Returns:
        DelayInfo object with parsed delay information
    """
    if parser is None:
        parser = _default_parser
    return parser.parse(message)


def detect_mta_from_entries(entries: list) -> Optional[str]:
    """Detect MTA type from log entries based on service names.

    Args:
        entries: List of log entries to analyze

    Returns:
        MTA type string ('postfix' or 'exim') or None if cannot be determined
    """
    for entry in entries:
        service = getattr(entry, "service", "").lower()
        if "postfix" in service:
            return "postfix"
        elif "exim" in service:
            return "exim"
    return None


def get_parser_for_mta(mta_type: Optional[str]) -> DelayParser:
    """Get the appropriate delay parser for an MTA type.

    Args:
        mta_type: MTA type string ('postfix' or 'exim')

    Returns:
        Appropriate DelayParser instance
    """
    if mta_type == "exim":
        return EximDelayParser()
    else:
        return PostfixDelayParser()
