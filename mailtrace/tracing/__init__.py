import logging
from datetime import datetime, timedelta
from time import sleep
from typing import Dict

from opentelemetry import trace

from mailtrace.config import Config
from mailtrace.models import LogEntry
from mailtrace.tracing.delay_parser import (
    DelayInfo,
    detect_mta_from_entries,
    get_parser_for_mta,
)
from mailtrace.tracing.otel import (
    create_delay_spans,
    create_host_span,
    create_root_span,
    dt_to_ns,
    flush_traces,
    init_exporter,
)
from mailtrace.tracing.query import (
    group_logs_by_hosts,
    group_logs_by_message_id,
    query_all_logs,
)

logger = logging.getLogger("mailtrace")


class EmailTracesGenerator:
    def __init__(self, config: Config, otel_endpoint: str) -> None:
        self.config = config
        self.otel_endpoint = otel_endpoint
        self.last_query_time = datetime.utcnow()
        init_exporter(otel_endpoint)

    def run(self, interval_seconds: int) -> None:
        try:
            while True:
                # Group logs by message_id and then by host_id
                query_end = datetime.utcnow()
                logs = query_all_logs(
                    self.config, self.last_query_time, query_end
                )
                logs_by_message_id = group_logs_by_message_id(logs)
                logs_by_host_id: Dict[str, Dict[str, list[LogEntry]]] = {}
                for message_id, message_id_logs in logs_by_message_id.items():
                    logs_by_host_id[message_id] = group_logs_by_hosts(
                        message_id_logs
                    )

                # Iterate over all emails
                for message_id, hosts_logs in logs_by_host_id.items():
                    hosts = list(hosts_logs.keys())
                    logger.debug(
                        f"Processing email with message_id {message_id} and hosts {hosts}"
                    )

                    host_info: dict[
                        str, tuple[DelayInfo, datetime, datetime]
                    ] = {}

                    # For each host, detect the MTA, parse the logs to extract delay info,
                    # and determine the start and end times for the host span
                    for host, host_logs in hosts_logs.items():
                        mta = detect_mta_from_entries(host_logs)
                        parser = get_parser_for_mta(mta)
                        delay_info = DelayInfo()
                        for log in host_logs:
                            delay_info |= parser.parse(log.message)
                        logger.debug(
                            f"Host {host} has delay info: {delay_info}"
                        )

                        # Start time: first log entry datetime
                        host_start = min(
                            datetime.fromisoformat(
                                log.datetime.replace("Z", "+00:00")
                            )
                            for log in host_logs
                        )
                        # host_end = ref_time + total_delay
                        host_end = host_start + timedelta(
                            seconds=delay_info.total_delay
                        )
                        host_info[host] = (delay_info, host_start, host_end)

                    if not host_info:
                        logger.debug(
                            f"No delay info found for message_id {message_id}, skipping"
                        )
                        continue

                    # Root span covers the full delivery window across all hosts
                    root_start = min(info[1] for info in host_info.values())
                    root_end = max(info[2] for info in host_info.values())

                    # Create spans
                    # Create root span
                    root_span = create_root_span(message_id, root_start)
                    root_ctx = trace.set_span_in_context(root_span)

                    # Create host spans and their child delay spans
                    for host, (
                        delays,
                        host_start,
                        host_end,
                    ) in host_info.items():
                        host_span = create_host_span(
                            host, host_start, root_ctx
                        )
                        host_ctx = trace.set_span_in_context(host_span)

                        # Create delay stage spans (siblings under the host span)
                        create_delay_spans(delays, host, host_start, host_ctx)

                        logger.debug(
                            f"Close host span: {host} at {host_end.isoformat()}"
                        )
                        host_span.end(end_time=dt_to_ns(host_end))

                    # End the root span last
                    root_span.end(end_time=int(root_end.timestamp() * 1e9))

                # Emit the traces to the OpenTelemetry collector
                flush_traces()

                # Update the last query time to the end of the current query
                self.last_query_time = query_end
                sleep(interval_seconds)
        except KeyboardInterrupt:
            print("EmailTracesGenerator stopped.")


__all__ = ["EmailTracesGenerator"]
