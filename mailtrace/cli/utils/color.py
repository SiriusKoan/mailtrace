"""Color printing utilities for CLI output."""


def print_blue(text: str) -> None:
    """Print text in blue color using ANSI escape codes."""
    print(f"\033[94m{text}\033[0m")


def print_red(text: str) -> None:
    """Print text in red color using ANSI escape codes."""
    print(f"\033[91m{text}\033[0m")
