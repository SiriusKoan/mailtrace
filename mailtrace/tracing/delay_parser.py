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
DELAY_STAGES = [
    "before_qmgr",
    "in_qmgr",
    "conn_setup",
    "transmission",
]


@dataclass
class DelayInfo:
    """Parsed delay information from a log message.

    Attributes:
        total_delay: Total message delay in seconds
        before_qmgr: Delay before queue manager
        in_qmgr: Delay in queue manager
        conn_setup: Delay in connection setup
        transmission: Delay in transmission
    """

    total_delay: Optional[float] = None
    before_qmgr: Optional[float] = None
    in_qmgr: Optional[float] = None
    conn_setup: Optional[float] = None
    transmission: Optional[float] = None

    def get_delay(self, stage_name: str) -> Optional[float]:
        """Get delay value for a specific stage name.

        Args:
            stage_name: The stage name (e.g., 'before_qmgr', 'in_qmgr')

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
