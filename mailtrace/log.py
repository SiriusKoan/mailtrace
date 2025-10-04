"""Logging configuration for the mailtrace application.

This module provides centralized logging setup and configuration
for consistent log output across the application with colored output.
"""

import logging

from colorama import Fore, Style, init

from mailtrace.config import Config

# Initialize colorama for cross-platform colored output
init(autoreset=True)

logger = logging.getLogger("mailtrace")


class ColoredFormatter(logging.Formatter):
    """Custom formatter that adds colors to log levels."""

    # Color mapping for different log levels
    COLORS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.BLUE,
        logging.WARNING: Fore.YELLOW,  # Orange-ish color
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.RED + Style.BRIGHT,
    }

    def format(self, record):
        # Get the original formatted message
        original_format = super().format(record)

        # Get the color for this log level
        color = self.COLORS.get(record.levelno, "")

        # Apply color only to the level name in the message
        if color:
            # Split the formatted message and colorize just the level name
            parts = original_format.split(
                " - ", 3
            )  # Split into: time, name, level, message
            if len(parts) >= 3:
                # Colorize the level name (third part)
                parts[2] = f"{color}{parts[2]}{Style.RESET_ALL}"
                return " - ".join(parts)

        return original_format


def init_logger(config: Config):
    """Initialize the logger with colored output."""
    log_level = config.log_level
    logger.setLevel(log_level)

    # Clear any existing handlers to avoid duplicates
    logger.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(log_level)

    # Use colored formatter
    formatter = ColoredFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    # Prevent duplicate logs from propagating to root logger
    logger.propagate = False
