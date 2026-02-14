"""CLI utilities for mailtrace."""

from .color import print_blue, print_red
from .password import handle_passwords, prompt_password
from .trace_base import (
    TraceStep,
    perform_trace_step,
    query_logs_from_aggregator,
)

__all__ = [
    "handle_passwords",
    "prompt_password",
    "print_blue",
    "print_red",
    "query_logs_from_aggregator",
    "perform_trace_step",
    "TraceStep",
]
