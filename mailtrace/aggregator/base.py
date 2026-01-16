import logging
from abc import ABC, abstractmethod
from typing import Any

from mailtrace.config import Config
from mailtrace.models import LogEntry, LogQuery
from mailtrace.utils import RelayResult

logger = logging.getLogger("mailtrace")


class LogAggregator(ABC):
    """Abstract base class for aggregating and querying log entries.

    This class defines the interface for log aggregation implementations
    that can query log entries based on specified criteria.
    """

    host: str
    config: Any

    @abstractmethod
    def __init__(self, host: str, config: Config):
        """Initialize the log aggregator with the specified host and configuration.

        Args:
            host (str): The hostname or identifier for the log source.
            config (Config): Configuration object containing connection and query settings.
        """

    @abstractmethod
    def query_by(self, query: LogQuery) -> list[LogEntry]:
        """Query log entries based on the provided query criteria.

        Args:
            query (LogQuery): The query object containing search criteria.

        Returns:
            list[LogEntry]: A list of log entries matching the query criteria.
        """

    def analyze_logs(self, log_entries: list[LogEntry]) -> RelayResult | None:
        """Analyze log entries to extract relay information.

        Examines SMTP/LMTP service log entries to check if relay information
        is available and returns the TraceResult.

        Args:
            log_entries (list[LogEntry]): List of log entries to analyze.

        Returns:
            TraceResult | None: A TraceResult object containing relay information if found,
                               None otherwise.
        """
        for log_entry in log_entries:
            # Check if relay information is available in the log entry
            if not log_entry.relay_host:
                continue

            logger.info(
                "Found relay %s [%s]:%d, new ID %s",
                log_entry.relay_host,
                log_entry.relay_ip,
                log_entry.relay_port,
                log_entry.mail_id,
            )

            return RelayResult(
                mail_id=log_entry.mail_id,
                relay_host=log_entry.relay_host,
                relay_ip=log_entry.relay_ip,
                relay_port=log_entry.relay_port,
                smtp_code=log_entry.smtp_code,
            )

        return None
