"""Error handling utilities for the mailtrace application.

This module provides centralized error handling functions to ensure
consistent, user-friendly error messages throughout the application.
"""

import sys
from typing import TypeVar

from mailtrace.exceptions import MailtraceError
from mailtrace.log import logger

T = TypeVar("T")


def handle_error(error: Exception, exit_on_error: bool = False) -> None:
    """Handle an error by logging it and optionally exiting.

    Args:
        error: The exception that was raised
        exit_on_error: If True, exit the program after logging the error
    """

    if isinstance(error, MailtraceError):
        logger.error(f"{error}")
    else:
        logger.error(f"Unexpected error: {error}")
        logger.debug("Stack trace:", exc_info=True)

    if exit_on_error:
        sys.exit(1)
