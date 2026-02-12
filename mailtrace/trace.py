"""
Core tracing functionality for mailtrace.

This module provides the main tracing functions that can be used both from
the CLI and as a library.
"""

import logging

from mailtrace.aggregator import do_trace
from mailtrace.aggregator.base import LogAggregator
from mailtrace.config import Config, Method
from mailtrace.graph import MailGraph
from mailtrace.models import LogQuery
from mailtrace.parser import LogEntry

logger = logging.getLogger("mailtrace")


def trace_mail_flow(
    trace_id: str,
    aggregator_class: type[LogAggregator],
    config: Config,
    host: str,
    graph: MailGraph,
) -> None:
    """
    Automatically trace the entire mail flow and build a graph.

    Follows mail hops from the starting host until no more relays are found.

    Args:
        trace_id: The initial mail ID to trace.
        aggregator_class: The aggregator class to instantiate for each hop.
        config: The configuration object for aggregator instantiation.
        host: The starting host where the mail was first found.
        graph: MailGraph instance to build the flow visualization.
    """
    aggregator = aggregator_class(host, config)
    current_host = host

    while True:
        result = do_trace(trace_id, aggregator)
        if result is None:
            logger.info("No more hops for %s", trace_id)
            break

        logger.info(
            "Relayed from %s to %s with new ID %s",
            current_host,
            result.relay_host,
            result.mail_id,
        )
        graph.add_hop(
            from_host=current_host,
            to_host=result.relay_host,
            queue_id=trace_id,
        )

        trace_id = result.mail_id
        current_host = result.relay_host
        aggregator = aggregator_class(current_host, config)


def _query_logs_from_aggregator(
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


def query_logs_by_keywords(
    config: Config,
    aggregator_class: type[LogAggregator],
    start_host: str,
    keywords: list[str],
    time: str,
    time_range: str,
) -> dict[str, tuple[str, list[LogEntry]]]:
    """
    Query logs by keywords and return mail IDs with their logs.

    Args:
        config: Configuration object containing connection settings.
        aggregator_class: The aggregator class to instantiate (SSHHost or OpenSearch).
        start_host: The starting host or cluster name to query.
        keywords: List of keywords to search for in log messages.
        time: Specific timestamp to filter by.
        time_range: Time range specification for filtering entries.

    Returns:
        Dictionary mapping mail IDs to (host, list of log entries).
    """
    logs_by_id: dict[str, tuple[str, list[LogEntry]]] = {}

    if config.method == Method.OPENSEARCH:
        aggregator = aggregator_class(start_host, config)
        logs_by_id = _query_logs_from_aggregator(
            aggregator, keywords, time, time_range
        )
    elif config.method == Method.SSH:
        hosts = config.cluster_to_hosts(start_host) or [start_host]
        logger.info("Using hosts: %s", hosts)
        for host in hosts:
            aggregator = aggregator_class(host, config)
            logs_by_id.update(
                _query_logs_from_aggregator(
                    aggregator, keywords, time, time_range
                )
            )

    if not logs_by_id:
        logger.info("No mail IDs found")

    return logs_by_id


def trace_mail_flow_to_file(
    config: Config,
    aggregator_class: type[LogAggregator],
    start_host: str,
    keywords: list[str],
    time: str,
    time_range: str,
    output_file: str | None = None,
) -> None:
    """
    Trace mail flow and save the graph to a Graphviz dot file.

    This is the main entry point for automated mail tracing that generates
    a complete graph of the mail delivery path.

    Args:
        config: Configuration object containing connection settings.
        aggregator_class: The aggregator class to instantiate (SSHHost or OpenSearch).
        start_host: The starting host or cluster name to query.
        keywords: List of keywords to search for in log messages.
        time: Specific timestamp to filter by.
        time_range: Time range specification for filtering entries.
        output_file: Optional output file path. If None or "-", writes to stdout.
    """
    logger.info("Querying logs by keywords...")
    logs_by_id = query_logs_by_keywords(
        config, aggregator_class, start_host, keywords, time, time_range
    )

    if not logs_by_id:
        logger.info("No mail IDs found to trace.")
        return

    logger.info("Found %d mail ID(s) to trace", len(logs_by_id))

    graph = MailGraph()
    for trace_id, (host_for_trace, _) in logs_by_id.items():
        logger.info("Tracing mail ID: %s", trace_id)
        trace_mail_flow(
            trace_id, aggregator_class, config, host_for_trace, graph
        )

    graph.to_dot(output_file)
    if output_file and output_file != "-":
        logger.info("Graph saved to %s", output_file)
    else:
        logger.info("Graph written to stdout")


__all__ = [
    "trace_mail_flow",
    "trace_mail_flow_to_file",
    "query_logs_by_keywords",
]
