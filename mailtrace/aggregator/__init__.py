from mailtrace.aggregator.base import LogAggregator, RelayResult
from mailtrace.aggregator.opensearch import OpenSearch
from mailtrace.aggregator.ssh_host import SSHHost
from mailtrace.log import logger
from mailtrace.models import LogQuery


def do_trace(mail_id: str, aggregator: LogAggregator) -> RelayResult | None:
    """
    Trace a mail message through Postfix logs to find the next relay hop and new mail ID.

    This function queries log entries for a given mail ID and analyzes them to determine
    where the mail was relayed and captures the response details in a TraceResult.
    All logs are printed before analysis is performed, and the function returns after
    all logs have been examined.

    Args:
        mail_id: The original mail ID to trace through the logs.
        aggregator: LogAggregator instance to query logs from.

    Returns:
        A TraceResult object containing:
            mail_id: The new mail ID assigned when queued at the next hop
            relay_host: Hostname of the relay host
            relay_ip: IP address of the relay host
            relay_port: Port number used for relaying
            smtp_code: The SMTP response code (typically 250)

        None if no relay entry is found.

    Example:
        >>> result = do_trace("ABC123", aggregator)
        >>> if result:
        ...     print(f"Mail relayed to {result.relay_host} with ID {result.mail_id}")
    """

    logger.info("Tracing mail ID: %s", mail_id)
    log_entries = aggregator.query_by(LogQuery(mail_id=mail_id))

    # Print all log entries
    print("=== Log Entries ===")
    for log_entry in log_entries:
        print(log_entry)

    # Analyze logs using the aggregator's analyze_logs method
    trace_result = aggregator.analyze_logs(log_entries)

    if trace_result is None:
        logger.info("No next hop found for %s", mail_id)

    return trace_result


__all__ = ["do_trace", "SSHHost", "OpenSearch", "RelayResult"]
