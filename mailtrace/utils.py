"""Utility functions for the mailtrace application.

This module provides various utility functions for validation,
data processing, and common operations used throughout the application.
"""

import datetime
import re

from mailtrace.exceptions import ValidationError


def time_validation(time: str, time_range: str) -> None:
    """
    Validate time and time_range parameters.

    Args:
        time: Time string in format YYYY-MM-DD HH:MM:SS
        time_range: Time range string in format [0-9]+[dhm] (days, hours, minutes)

    Raises:
        ValidationError: If validation fails
    """

    if time:
        time_pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        if not time_pattern.match(time):
            raise ValidationError(
                f"Time {time} should be in format YYYY-MM-DD HH:MM:SS",
                "Use format YYYY-MM-DD HH:MM:SS for time",
            )
    if time and not time_range or time_range and not time:
        raise ValidationError(
            "Time and time-range must be provided together",
            "Provide both time and time_range or neither",
        )
    time_range_pattern = re.compile(r"^\d+[dhm]$")
    if time_range and not time_range_pattern.match(time_range):
        raise ValidationError(
            "time_range should be in format [0-9]+[dhm]",
            "Use [0-9]+[dhm] for time range (e.g., 1d, 10h, 30m)",
        )


def time_range_to_timedelta(time_range: str) -> datetime.timedelta:
    """
    Convert a time range string to a datetime.timedelta object.

    Args:
        time_range: Time range string in format [0-9]+[dhm] where:
                   - d = days
                   - h = hours
                   - m = minutes

    Returns:
        datetime.timedelta object representing the time range

    Raises:
        ValidationError: If time_range format is invalid
    """
    # Validate format first
    if not time_range or len(time_range) < 2:
        raise ValidationError(
            f"Invalid time range format: {time_range}",
            "Time range should be in format [0-9]+[dhm] (e.g., 1d, 10h, 30m)",
        )

    unit = time_range[-1]
    value_str = time_range[:-1]

    if not value_str.isdigit():
        raise ValidationError(
            f"Invalid time range format: {time_range}",
            "Time range should be in format [0-9]+[dhm] (e.g., 1d, 10h, 30m)",
        )

    value = int(value_str)

    if unit == "d":
        return datetime.timedelta(days=value)
    elif unit == "h":
        return datetime.timedelta(hours=value)
    elif unit == "m":
        return datetime.timedelta(minutes=value)
    else:
        raise ValidationError(
            f"Invalid time range unit: {unit}",
            "Time range unit must be 'd' (days), 'h' (hours), or 'm' (minutes)",
        )


def print_blue(text: str):
    """
    Print text in blue color using ANSI escape codes.

    Args:
        text: The text to print in blue
    """

    print(f"\033[94m{text}\033[0m")


def print_red(text: str):
    """
    Print text in red color using ANSI escape codes.

    Args:
        text: The text to print in red
    """

    print(f"\033[91m{text}\033[0m")
