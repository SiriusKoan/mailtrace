"""CLI tracing module - glue layer between CLI and core tracing functionality.

This module provides the CLI command entry point for continuous tracing.
It orchestrates the workflow by calling functions from mailtrace.tracing.

Structure:
- main.py: run_continuous_tracing() - the main CLI command runner

This follows the same pattern as mailtrace.cli.run (CLI) vs mailtrace.aggregator (core).
All core tracing logic lives in mailtrace.tracing; this module just wires it up for CLI use.
"""

from mailtrace.cli.tracing.main import run_continuous_tracing

__all__ = ["run_continuous_tracing"]
