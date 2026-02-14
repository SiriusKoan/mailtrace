"""CLI module for mailtrace - command-line interface utilities and commands."""

from .graph import (
    MailGraph,
    query_logs_by_keywords,
    trace_mail_flow,
    trace_mail_flow_to_file,
)
from .run import print_logs_by_id, trace_mail_loop
from .utils import handle_passwords, print_blue, print_red, prompt_password

__all__ = [
    # Graph functions
    "MailGraph",
    "trace_mail_flow",
    "trace_mail_flow_to_file",
    "query_logs_by_keywords",
    # Run functions
    "trace_mail_loop",
    "print_logs_by_id",
    # Utils
    "handle_passwords",
    "prompt_password",
    "print_blue",
    "print_red",
]
