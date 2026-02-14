import logging

import click

from mailtrace.aggregator import select_aggregator
from mailtrace.cli import (
    handle_passwords,
    print_logs_by_id,
    query_logs_by_keywords,
    trace_mail_flow_to_file,
    trace_mail_loop,
)
from mailtrace.config import Config, load_config
from mailtrace.utils import time_validation

logger = logging.getLogger("mailtrace")


def configure_logging(config: Config) -> None:
    """
    Configure logging based on the config file settings.

    Args:
        config: Configuration object containing log_level setting.
    """
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


# Common CLI options shared between trace and run commands
COMMON_OPTIONS = [
    click.option(
        "-c",
        "--config-path",
        "config_path",
        type=click.Path(exists=True),
        required=False,
        help="Path to configuration file",
    ),
    click.option(
        "-h",
        "--start-host",
        type=str,
        required=True,
        help="The starting host or cluster name",
    ),
    click.option(
        "-k",
        "--key",
        type=str,
        required=True,
        help="The keyword, can be email address, domain, etc.",
        multiple=True,
    ),
    click.option(
        "--login-pass", type=str, required=False, help="The login password"
    ),
    click.option(
        "--sudo-pass", type=str, required=False, help="The sudo password"
    ),
    click.option(
        "--opensearch-pass",
        type=str,
        required=False,
        help="The opensearch password",
    ),
    click.option(
        "--ask-login-pass", is_flag=True, help="Ask for login password"
    ),
    click.option(
        "--ask-sudo-pass", is_flag=True, help="Ask for sudo password"
    ),
    click.option(
        "--ask-opensearch-pass",
        is_flag=True,
        help="Ask for opensearch password",
    ),
    click.option("--time", type=str, required=True, help="The time"),
    click.option(
        "--time-range",
        type=str,
        required=True,
        help="The time range, e.g. 1d, 10m",
    ),
]


def add_common_options(func):
    """Decorator to add common CLI options to a command."""
    for option in reversed(COMMON_OPTIONS):
        func = option(func)
    return func


@click.group()
def cli():
    pass


@cli.command()
@add_common_options
def run(
    config_path: str | None,
    start_host: str,
    key: list[str],
    login_pass: str | None,
    sudo_pass: str | None,
    opensearch_pass: str | None,
    ask_login_pass: bool,
    ask_sudo_pass: bool,
    ask_opensearch_pass: bool,
    time: str,
    time_range: str,
) -> None:
    """Interactively trace email messages through mail server logs."""
    config = load_config(config_path)
    configure_logging(config)
    handle_passwords(
        config,
        ask_login_pass,
        login_pass,
        ask_sudo_pass,
        sudo_pass,
        ask_opensearch_pass,
        opensearch_pass,
    )
    validation_error = time_validation(time, time_range)
    if validation_error:
        raise ValueError(validation_error)

    logger.info("Running mailtrace...")
    aggregator_class = select_aggregator(config)
    logs_by_id = query_logs_by_keywords(
        config, aggregator_class, start_host, list(key), time, time_range
    )
    print_logs_by_id(logs_by_id)

    if not logs_by_id:
        logger.info("No mail IDs found to trace.")
        return

    trace_id = input("Enter trace ID: ")
    if trace_id not in logs_by_id:
        logger.info(f"Trace ID {trace_id} not found in logs")
        return

    host_for_trace = logs_by_id[trace_id][0]
    trace_mail_loop(
        trace_id, logs_by_id, aggregator_class, config, host_for_trace
    )


@cli.command()
@add_common_options
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    required=False,
    default=None,
    help='Output file for the Graphviz dot graph (use "-" or omit for stdout)',
)
def graph(
    config_path: str | None,
    start_host: str,
    key: list[str],
    login_pass: str | None,
    sudo_pass: str | None,
    opensearch_pass: str | None,
    ask_login_pass: bool,
    ask_sudo_pass: bool,
    ask_opensearch_pass: bool,
    time: str,
    time_range: str,
    output: str | None,
) -> None:
    """Trace email messages and generate a Graphviz dot file."""
    config = load_config(config_path)
    configure_logging(config)
    handle_passwords(
        config,
        ask_login_pass,
        login_pass,
        ask_sudo_pass,
        sudo_pass,
        ask_opensearch_pass,
        opensearch_pass,
    )
    validation_error = time_validation(time, time_range)
    if validation_error:
        raise ValueError(validation_error)

    logger.info("Running mailtrace...")
    aggregator_class = select_aggregator(config)
    trace_mail_flow_to_file(
        config=config,
        aggregator_class=aggregator_class,
        start_host=start_host,
        keywords=list(key),
        time=time,
        time_range=time_range,
        output_file=output,
    )


if __name__ == "__main__":
    cli()
