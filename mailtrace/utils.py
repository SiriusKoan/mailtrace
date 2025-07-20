import datetime
import re


def time_validation(time: str, time_range: str) -> str:
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
    if time_range.endswith("d"):
        return datetime.timedelta(days=int(time_range[:-1]))
    if time_range.endswith("h"):
        return datetime.timedelta(hours=int(time_range[:-1]))
    if time_range.endswith("m"):
        return datetime.timedelta(minutes=int(time_range[:-1]))
    raise ValueError("Invalid time range")
