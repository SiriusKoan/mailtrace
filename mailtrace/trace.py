"""
Core tracing functionality for mailtrace.

This module provides the main tracing functions that can be used both from
the CLI and as a library.
"""

import logging

from mailtrace.aggregator import (
    _RELAY_SERVICES,
    _parse_relay_info,
    do_trace,
    extract_message_ids,
)
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
            # Retry without hostname filter (some machines lack proper reverse DNS)
            aggregator = aggregator_class("", config)
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


def _build_hostname_map(
    all_logs: list[LogEntry],
) -> dict[str, str]:
    """Build a map from short hostname to the canonical log hostname.

    Relay entries use FQDNs (e.g. mailer4.test.cc.cs.nctu.edu.tw) but
    log entries use short names (e.g. mailer4).  This map lets us
    normalize relay hostnames to match the log hostnames so the graph
    nodes connect properly.
    """
    hostname_map: dict[str, str] = {}
    for entry in all_logs:
        if entry.hostname:
            short = entry.hostname.split(".")[0]
            hostname_map[short] = entry.hostname
            hostname_map[entry.hostname] = entry.hostname
    return hostname_map


def _normalize_host(host: str, hostname_map: dict[str, str]) -> str:
    """Resolve a relay hostname to the canonical log hostname."""
    short = host.split(".")[0]
    return hostname_map.get(short, host)


def _reconstruct_chain(all_logs: list[LogEntry], graph: MailGraph) -> None:
    """Reconstruct the mail flow graph from a batch of logs.

    Groups logs by queue_id and finds relay hops to build the graph.
    """
    hostname_map = _build_hostname_map(all_logs)

    # Group logs by queue_id
    logs_by_qid: dict[str, list[LogEntry]] = {}
    for entry in all_logs:
        if entry.mail_id:
            logs_by_qid.setdefault(entry.mail_id, []).append(entry)

    # For each queue_id group, find relay entries
    for qid, entries in logs_by_qid.items():
        hostname = entries[0].hostname if entries else None
        if not hostname:
            continue

        for entry in entries:
            if entry.service not in _RELAY_SERVICES:
                continue
            result = _parse_relay_info(entry)
            if result and result.relay_host:
                to_host = _normalize_host(result.relay_host, hostname_map)
                logger.info(
                    "Batch trace: %s relayed from %s to %s",
                    qid,
                    hostname,
                    to_host,
                )
                graph.add_hop(
                    from_host=hostname,
                    to_host=to_host,
                    queue_id=qid,
                )


def trace_mail_flow_by_message_id(
    message_id: str,
    aggregator: LogAggregator,
    graph: MailGraph,
) -> list[LogEntry]:
    """Trace mail by message_id in a single batch query.

    Queries all logs matching the message_id across all hosts,
    then reconstructs the chain from the batch results.

    Args:
        message_id: RFC 2822 Message-ID to trace.
        aggregator: LogAggregator instance for querying.
        graph: MailGraph instance to build the flow visualization.

    Returns:
        All log entries found for this message_id.
    """
    logger.info("Batch tracing message_id: %s", message_id)
    all_logs = aggregator.query_by(LogQuery(message_id=message_id))
    logger.debug(
        "Found %d log entries for message_id %s", len(all_logs), message_id
    )
    _reconstruct_chain(all_logs, graph)
    return all_logs


def _query_logs_batch(
    aggregator: LogAggregator,
    keywords: list[str],
    time: str,
    time_range: str,
) -> dict[str, tuple[str, list[LogEntry]]]:
    """Query logs using Message-ID + batch optimization.

    Works uniformly for both OpenSearch and SSH backends:
    1. Keyword search (time-restricted) → initial queue-IDs + Message-IDs
    2. Message-ID queries (no time restriction) → all related queue-IDs
    3. Batch fetch ALL queue-IDs in one query (no time restriction)

    Args:
        aggregator: The aggregator instance to query logs from.
        keywords: List of keywords to search for in log messages.
        time: Specific timestamp to filter by.
        time_range: Time range specification for filtering entries.

    Returns:
        Dictionary mapping mail IDs to (actual_host, log_entries).
    """
    # Step 1: keyword search (time-restricted)
    base_logs = aggregator.query_by(
        LogQuery(keywords=keywords, time=time, time_range=time_range)
    )
    initial_mail_ids = list(
        {log.mail_id for log in base_logs if log.mail_id is not None}
    )
    if not initial_mail_ids:
        return {}

    # Step 2: find ALL related queue-IDs via Message-ID (no time restriction)
    message_ids = extract_message_ids(base_logs)
    all_queue_ids = set(initial_mail_ids)

    for mid in message_ids:
        logger.info("Querying by message_id: %s", mid)
        mid_logs = aggregator.query_by(LogQuery(message_id=mid))
        all_queue_ids.update(log.mail_id for log in mid_logs if log.mail_id)

    # Step 3: batch fetch ALL queue-IDs at once (no time restriction)
    logs_by_id: dict[str, tuple[str, list[LogEntry]]] = {}
    batch_logs = aggregator.query_by(LogQuery(mail_ids=list(all_queue_ids)))
    for entry in batch_logs:
        if entry.mail_id and entry.mail_id in all_queue_ids:
            logs_by_id.setdefault(entry.mail_id, (entry.hostname, []))[
                1
            ].append(entry)

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

    Uses Message-ID batch optimization for both OpenSearch and SSH backends.

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
        logs_by_id = _query_logs_batch(aggregator, keywords, time, time_range)
    elif config.method == Method.SSH:
        hosts = config.cluster_to_hosts(start_host) or [start_host]
        logger.info("Using hosts: %s", hosts)
        for host in hosts:
            aggregator = aggregator_class(host, config)
            logs_by_id.update(
                _query_logs_batch(aggregator, keywords, time, time_range)
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

    Uses batch-fetched logs from query_logs_by_keywords to reconstruct
    the graph without additional queries.

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

    # Reconstruct the graph from already-fetched logs (no extra queries)
    all_logs = [
        entry
        for _, log_entries in logs_by_id.values()
        for entry in log_entries
    ]
    _reconstruct_chain(all_logs, graph)

    graph.to_dot(output_file)
    if output_file and output_file != "-":
        logger.info("Graph saved to %s", output_file)
    else:
        logger.info("Graph written to stdout")


__all__ = [
    "trace_mail_flow",
    "trace_mail_flow_by_message_id",
    "trace_mail_flow_to_file",
    "query_logs_by_keywords",
]
