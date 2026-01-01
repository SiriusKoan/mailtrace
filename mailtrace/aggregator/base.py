from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from mailtrace.config import Config
from mailtrace.models import LogEntry, LogQuery


@dataclass
class TraceResult:
    mail_id: str
    relay_host: str
    relay_ip: str
    relay_port: int
    smtp_code: int


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

    @abstractmethod
    def analyze_logs(self, log_entries: list[LogEntry]) -> TraceResult | None:
        """Analyze log entries to extract relay information.

        Args:
            log_entries (list[LogEntry]): List of log entries to analyze.

        Returns:
            TraceResult | None: A TraceResult object containing relay information if found,
                               None otherwise.
        """
