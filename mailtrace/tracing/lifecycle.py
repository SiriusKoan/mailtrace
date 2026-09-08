"""Trace buffering and terminal-outcome decisions."""

from dataclasses import dataclass
from math import ceil
from typing import Callable

from mailtrace.models import LogEntry


@dataclass
class PendingTrace:
    """Logs buffered until a message reaches a terminal outcome."""

    logs: list[LogEntry]
    first_seen_round: int
    last_seen_round: int
    has_terminal_outcome: bool = False

    def merge(
        self,
        new_logs: list[LogEntry],
        current_round: int,
        log_key: Callable[[LogEntry], tuple],
    ) -> int:
        """Add unseen logs and refresh the round only when logs are added."""
        seen_keys = {log_key(log) for log in self.logs}
        additions = [log for log in new_logs if log_key(log) not in seen_keys]
        self.logs.extend(additions)
        if additions:
            self.last_seen_round = current_round
            self.has_terminal_outcome |= any(
                log.is_terminal is True for log in additions
            )
        return len(additions)


def should_export_trace(
    pending: PendingTrace,
    current_round: int,
    sleep_seconds: int,
    hold_rounds: int,
    max_trace_age_seconds: int,
) -> bool:
    """Return whether a pending trace reached a safe export boundary."""
    quiet_rounds = current_round - pending.last_seen_round
    age_rounds = current_round - pending.first_seen_round
    max_age_rounds = max(1, ceil(max_trace_age_seconds / sleep_seconds))

    if age_rounds >= max_age_rounds:
        return True
    return pending.has_terminal_outcome and quiet_rounds >= hold_rounds
