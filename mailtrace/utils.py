import datetime


def time_range_to_timedelta(time_range: str) -> datetime.timedelta:
    if time_range.endswith("d"):
        return datetime.timedelta(days=int(time_range[:-1]))
    if time_range.endswith("h"):
        return datetime.timedelta(hours=int(time_range[:-1]))
    if time_range.endswith("m"):
        return datetime.timedelta(minutes=int(time_range[:-1]))
    raise ValueError("Invalid time range")
