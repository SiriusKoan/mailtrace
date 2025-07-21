from abc import ABC, abstractmethod

from mailtrace.models import LogEntry, LogQuery


class LogAggregator(ABC):
    """Abstract base class for aggregating and querying log entries.

    This class defines the interface for log aggregation implementations
    that can query log entries based on specified criteria.
    """

    @abstractmethod
    def query_by(self, query: LogQuery) -> list[LogEntry]:
        """Query log entries based on the provided query criteria.

        Args:
            query (LogQuery): The query object containing search criteria.

        Returns:
            list[LogEntry]: A list of log entries matching the query criteria.
        """
