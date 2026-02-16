"""Interactive mail tracing functionality for the 'run' command."""

import logging
import sys

from mailtrace.aggregator.base import LogAggregator
from mailtrace.config import Config
from mailtrace.parser import LogEntry

from ..utils import perform_trace_step, print_blue

logger = logging.getLogger("mailtrace")


def print_logs_by_id(
    logs_by_id: dict[str, tuple[str, list[LogEntry]]],
) -> None:
    """
    Print logs grouped by mail ID.

    Args:
        logs_by_id: Dictionary mapping mail IDs to (host, list of LogEntry) tuples.
    """
    for mail_id, (_, logs) in logs_by_id.items():
        print_blue(f"== Mail ID: {mail_id} ==")
        for log in logs:
            print(str(log))
        print_blue("==============\n")
    sys.stdout.flush()


def trace_mail_loop(
    trace_id: str,
    logs_by_id: dict[str, tuple[str, list[LogEntry]]],
    aggregator_class: type[LogAggregator],
    config: Config,
    host: str,
) -> None:
    """
    Interactively trace mail hops starting from the given trace ID.

    This function allows manual navigation through the mail flow, with options
    to continue to the next hop, stay on the local host, or jump to a specific host.

    Args:
        trace_id: The initial mail ID to trace.
        logs_by_id: Dictionary mapping mail IDs to lists of LogEntry objects.
        aggregator_class: The aggregator class to instantiate for each hop.
        config: The configuration object for aggregator instantiation.
        host: The current host.
    """
    if trace_id not in logs_by_id:
        logger.info(f"Trace ID {trace_id} not found in logs")
        return

    aggregator = aggregator_class(host, config)

    while True:
        step = perform_trace_step(trace_id, aggregator)
        if step is None:
            logger.info("No more hops")
            break

        print_blue(
            f"Relayed to {step.relay_host} ({step.relay_ip}:{step.relay_port}) "
            f"with new ID {step.trace_id} (SMTP {step.smtp_code})"
        )

        # If auto_continue is enabled, automatically continue to the next hop
        if config.auto_continue:
            logger.info(
                f"Auto-continue enabled. Continuing to {step.relay_host}"
            )
            trace_next_hop_ans = "y"
        else:
            trace_next_hop_ans: str = input(
                f"Trace next hop: {step.relay_host}? (Y/n/local/<next hop>): "
            ).lower()

        if trace_next_hop_ans in ["", "y"]:
            trace_id = step.trace_id
            aggregator = aggregator_class(step.relay_host, config)
        elif trace_next_hop_ans == "n":
            logger.info("Trace stopped")
            break
        elif trace_next_hop_ans == "local":
            trace_id = step.trace_id
            aggregator = aggregator_class(host, config)
        else:
            trace_id = step.trace_id
            aggregator = aggregator_class(trace_next_hop_ans, config)


__all__ = [
    "trace_mail_loop",
    "print_logs_by_id",
]
