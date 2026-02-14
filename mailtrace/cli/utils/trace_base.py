"""
Base trace utilities shared between graph and run modules.

This module contains common tracing logic used by both interactive (run)
and automated (graph) mail flow tracing.
"""

import logging

from mailtrace.aggregator import do_trace
from mailtrace.aggregator.base import LogAggregator
from mailtrace.models import LogQuery
from mailtrace.parser import LogEntry

logger = logging.getLogger("mailtrace")


def query_logs_from_aggregator(
    aggregator: LogAggregator,
    keywords: list[str],
    time: str,
    time_range: str,
) -> dict[str, tuple[str, list[LogEntry]]]:
    """
    Query logs from a single aggregator and return mail IDs with their logs.

    Args:
        aggregator: The aggregator instance to query logs from.
        keywords: List of keywords to search for in log messages.
        time: Specific timestamp to filter by.
        time_range: Time range specification for filtering entries.

    Returns:
        Dictionary mapping mail IDs to (actual_host, log_entries).
    """
    base_logs = aggregator.query_by(
        LogQuery(keywords=keywords, time=time, time_range=time_range)
    )
    mail_ids = list(
        {log.mail_id for log in base_logs if log.mail_id is not None}
    )

    logs_by_id: dict[str, tuple[str, list[LogEntry]]] = {}
    for mail_id in mail_ids:
        mail_logs = aggregator.query_by(LogQuery(mail_id=mail_id))
        actual_host = mail_logs[0].hostname if mail_logs else aggregator.host
        logs_by_id[mail_id] = (actual_host, mail_logs)

    return logs_by_id


class TraceStep:
    """
    Represents a single trace step result.

    Encapsulates the information returned from a trace operation.
    """

    def __init__(
        self,
        trace_id: str,
        relay_host: str,
        smtp_code: int | None = None,
        relay_ip: str | None = None,
        relay_port: str | None = None,
    ):
        self.trace_id = trace_id
        self.relay_host = relay_host
        self.smtp_code = smtp_code
        self.relay_ip = relay_ip
        self.relay_port = relay_port

    @classmethod
    def from_do_trace_result(cls, result):
        """
        Create a TraceStep from a do_trace result object.

        Args:
            result: Result object from do_trace function.

        Returns:
            TraceStep instance or None if result is None.
        """
        if result is None:
            return None
        return cls(
            trace_id=result.mail_id,
            relay_host=result.relay_host,
            smtp_code=(
                result.smtp_code if hasattr(result, "smtp_code") else None
            ),
            relay_ip=result.relay_ip if hasattr(result, "relay_ip") else None,
            relay_port=(
                result.relay_port if hasattr(result, "relay_port") else None
            ),
        )


def perform_trace_step(
    trace_id: str,
    aggregator: LogAggregator,
) -> TraceStep | None:
    """
    Perform a single trace step and return normalized result.

    This is a helper function that wraps do_trace and handles the conversion
    to a TraceStep object, reducing duplication between graph and run modules.

    Args:
        trace_id: The mail ID to trace.
        aggregator: The aggregator instance to query from.

    Returns:
        TraceStep object or None if no more hops.
    """
    result = do_trace(trace_id, aggregator)
    return TraceStep.from_do_trace_result(result)


__all__ = [
    "query_logs_from_aggregator",
    "perform_trace_step",
    "TraceStep",
]
