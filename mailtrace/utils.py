import datetime
import re


def time_validation(time: str, time_range: str) -> str:
    """
    Validate time and time_range parameters.

    Args:
        time: Time string in format YYYY-MM-DD HH:MM:SS
        time_range: Time range string in format [0-9]+[dhm] (days, hours, minutes)

    Returns:
        Empty string if validation passes, error message if validation fails
    """

    if time:
        time_pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        if not time_pattern.match(time):
            return f"Time {time} should be in format YYYY-MM-DD HH:MM:SS"
    if time and not time_range or time_range and not time:
        return "Time and time-range must be provided together"
    time_range_pattern = re.compile(r"^\d+[dhm]$")
    if time_range and not time_range_pattern.match(time_range):
        return "time_range should be in format [0-9]+[dhm]"
    return ""


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
        ValueError: If time_range format is invalid
    """

    if time_range.endswith("d"):
        return datetime.timedelta(days=int(time_range[:-1]))
    if time_range.endswith("h"):
        return datetime.timedelta(hours=int(time_range[:-1]))
    if time_range.endswith("m"):
        return datetime.timedelta(minutes=int(time_range[:-1]))
    raise ValueError("Invalid time range")


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
