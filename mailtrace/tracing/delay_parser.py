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


@dataclass
class DelayInfo:
    """Parsed delay information from a log message.

    Attributes:
        before_qmgr: Delay before queue manager (Postfix)
        in_qmgr: Delay in queue manager (Postfix)
        conn_setup: Delay in connection setup (Postfix)
        transmission: Delay in transmission (Postfix)
        queue_time: Queue time (Exim)
        receive_time: Receive time (Exim)
        deliver_time: Delivery time (Exim)
    """

    # Postfix delays
    before_qmgr: Optional[float] = None
    in_qmgr: Optional[float] = None
    conn_setup: Optional[float] = None
    transmission: Optional[float] = None
    # Exim delays
    queue_time: Optional[float] = None
    receive_time: Optional[float] = None
    deliver_time: Optional[float] = None

    def __or__(self, value):
        if isinstance(value, DelayInfo):
            return DelayInfo(
                before_qmgr=(
                    self.before_qmgr
                    if self.before_qmgr is not None
                    else value.before_qmgr
                ),
                in_qmgr=(
                    self.in_qmgr if self.in_qmgr is not None else value.in_qmgr
                ),
                conn_setup=(
                    self.conn_setup
                    if self.conn_setup is not None
                    else value.conn_setup
                ),
                transmission=(
                    self.transmission
                    if self.transmission is not None
                    else value.transmission
                ),
                queue_time=(
                    self.queue_time
                    if self.queue_time is not None
                    else value.queue_time
                ),
                receive_time=(
                    self.receive_time
                    if self.receive_time is not None
                    else value.receive_time
                ),
                deliver_time=(
                    self.deliver_time
                    if self.deliver_time is not None
                    else value.deliver_time
                ),
            )
        return self

    @property
    def total_delay(self) -> float:
        """Calculate total delay based on available stage delays.

        For Postfix, total_delay is the sum of all stages.
        For Exim, total_delay is the queue_time + receive_time + deliver_time.

        Returns:
            Total delay in seconds, or None if not enough information
        """
        if all(
            getattr(self, stage) is not None for stage in POSTFIX_DELAY_STAGES
        ):
            return sum(getattr(self, stage) for stage in POSTFIX_DELAY_STAGES)
        elif all(
            getattr(self, stage) is not None for stage in EXIM_DELAY_STAGES
        ):
            return sum(getattr(self, stage) for stage in EXIM_DELAY_STAGES)
        else:
            return 0

    def get_delay_values(self) -> dict[str, float]:
        """Get a dictionary of all delay stage values.

        Returns:
            Dictionary mapping stage names to their delay values
        """
        if all(getattr(self, s, 0) is not None for s in POSTFIX_DELAY_STAGES):
            stage_names = POSTFIX_DELAY_STAGES
        elif all(getattr(self, s, 0) is not None for s in EXIM_DELAY_STAGES):
            stage_names = EXIM_DELAY_STAGES
        else:
            return {}

        return {s: float(getattr(self, s, 0)) for s in stage_names}


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
        qt = 0
        if qt_match:
            qt = float(qt_match.group(1))

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
            delay_info.queue_time = max(0.0, qt - rt - dt)

        return delay_info

    def get_mta_type(self) -> str:
        """Get the MTA type for this parser."""
        return "exim"

    def get_delay_stages(self) -> list[str]:
        """Get the delay stages for this parser."""
        return EXIM_DELAY_STAGES


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
