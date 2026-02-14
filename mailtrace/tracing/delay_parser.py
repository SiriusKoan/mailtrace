"""Delay information parser for email trace entries."""

import re
from typing import Dict, Optional


def parse_delay_info(message: str) -> Dict[str, Optional[float]]:
    """Parse delay information from a log message.

    Extracts:
    - delay: Total message delay in seconds
    - delay_before_qmgr: Delay before queue manager
    - delay_in_qmgr: Delay in queue manager
    - delay_conn_setup: Delay in connection setup
    - delay_transmission: Delay in transmission

    Args:
        message: The log message to parse

    Returns:
        Dictionary with parsed delay information (None for missing fields)
    """
    delays: Dict[str, Optional[float]] = {
        "delay": None,
        "delay_before_qmgr": None,
        "delay_in_qmgr": None,
        "delay_conn_setup": None,
        "delay_transmission": None,
    }

    # Parse total delay
    delay_match = re.search(r"delay=([\d.]+)", message)
    if delay_match:
        delays["delay"] = float(delay_match.group(1))

    # Parse delays breakdown (delays=before/qmgr/setup/transmission)
    delays_match = re.search(
        r"delays=([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", message
    )
    if delays_match:
        delays["delay_before_qmgr"] = float(delays_match.group(1))
        delays["delay_in_qmgr"] = float(delays_match.group(2))
        delays["delay_conn_setup"] = float(delays_match.group(3))
        delays["delay_transmission"] = float(delays_match.group(4))

    return delays
