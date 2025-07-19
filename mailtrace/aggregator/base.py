from abc import ABC, abstractmethod

from mailtrace.models import LogEntry, LogQuery


class LogAggregator(ABC):
    @abstractmethod
    def query_by(self, query: LogQuery) -> list[LogEntry]:
        pass
