"""Command-line interface for the mailtrace application.

This module provides the main CLI entry point and commands for tracing
email messages through mail server logs using various aggregation methods.
"""

import getpass
from typing import Tuple, Type

import click

from mailtrace.aggregator import OpenSearch, SSHHost, do_trace
from mailtrace.aggregator.base import LogAggregator
from mailtrace.config import Config, Method, load_config
from mailtrace.error_handler import handle_error
from mailtrace.exceptions import (
    ConfigurationError,
    MailtraceError,
)
from mailtrace.log import init_logger, logger
from mailtrace.models import LogQuery
from mailtrace.parser import LogEntry
from mailtrace.utils import print_blue, time_validation


@click.group()
def cli():
    pass


def handle_passwords(
    config: Config,
    ask_login_pass: bool,
    login_pass: str | None,
    ask_sudo_pass: bool,
    sudo_pass: str | None,
    ask_opensearch_pass: bool,
    opensearch_pass: str | None,
) -> None:
    """
    Handles password input and assignment for SSH, sudo, and OpenSearch connections.

    Prompts the user for passwords if requested, assigns them to the config, and logs warnings for empty passwords.

    Args:
        config: The configuration object containing connection settings.
        ask_login_pass: Boolean, whether to prompt for login password.
        login_pass: The login password (may be None).
        ask_sudo_pass: Boolean, whether to prompt for sudo password.
        sudo_pass: The sudo password (may be None).
        ask_opensearch_pass: Boolean, whether to prompt for OpenSearch password.
        opensearch_pass: The OpenSearch password (may be None).
    """

    # Check method before handling passwords
    if config.method == Method.SSH:
        # login pass
        if ask_login_pass:
            login_pass = getpass.getpass(prompt="Enter login password: ")
        config.ssh_config.password = login_pass or config.ssh_config.password
        if not login_pass:
            logger.warning(
                "Warning: empty login password is provided, no password will be used for login"
            )

        # sudo pass
        if ask_sudo_pass:
            sudo_pass = getpass.getpass(prompt="Enter sudo password: ")
        config.ssh_config.sudo_pass = sudo_pass or config.ssh_config.sudo_pass
        if not sudo_pass:
            logger.warning(
                "Warning: empty sudo password is provided, no password will be used for sudo"
            )

    elif config.method == Method.OPENSEARCH:
        # opensearch pass
        if ask_opensearch_pass:
            opensearch_pass = getpass.getpass(
                prompt="Enter opensearch password: "
            )
        config.opensearch_config.password = (
            opensearch_pass or config.opensearch_config.password
        )
        if not opensearch_pass:
            logger.warning(
                "Warning: empty opensearch password is provided, no password will be used for opensearch"
            )
    else:
        logger.warning(
            f"Unknown method: {config.method}. No password handling performed."
        )


def select_aggregator(config: Config) -> Type[LogAggregator]:
    """
    Selects and returns the appropriate log aggregator class based on the config method.

    Args:
        config: The configuration object containing the method attribute.

    Returns:
        The aggregator class (SSHHost or OpenSearch).

    Raises:
        ConfigurationError: If the method is unsupported.
    """

    if config.method == Method.SSH:
        return SSHHost
    elif config.method == Method.OPENSEARCH:
        return OpenSearch
    else:
        raise ConfigurationError(
            f"Unsupported connection method: {config.method}",
            f"Valid methods are: {', '.join([m.value for m in Method])}",
        )


def query_and_print_logs(
    aggregator: LogAggregator,
    key: list[str],
    time: str,
    time_range: str,
) -> dict[str, Tuple[str, list[LogEntry]]]:
    """
    Queries logs using the aggregator and prints logs grouped by mail ID.

    Args:
        aggregator: The aggregator instance to query logs.
        key: Keywords for the log query.
        time: Specific time for the log query.
        time_range: Time range for the log query.

    Returns:
        logs_by_id: Dictionary mapping mail IDs to lists of LogEntry objects.
    """

    try:
        base_logs = aggregator.query_by(
            LogQuery(keywords=key, time=time, time_range=time_range)
        )
    except MailtraceError as e:
        handle_error(e)
        return {}

    ids = list({log.mail_id for log in base_logs if log.mail_id is not None})
    if not ids:
        logger.info("No mail IDs found")
        return {}
    logs_by_id: dict[str, Tuple[str, list[LogEntry]]] = {}
    for mail_id in ids:
        try:
            logs_by_id[mail_id] = (
                aggregator.host,
                aggregator.query_by(LogQuery(mail_id=mail_id)),
            )
            print_blue(f"== Mail ID: {mail_id} ==")
            for log in logs_by_id[mail_id][1]:
                print(str(log))
            print_blue("==============\n")
        except MailtraceError as e:
            logger.warning(
                f"Failed to retrieve logs for mail ID {mail_id}: {e}"
            )
            continue
    return logs_by_id


def trace_mail_loop(
    trace_id: str,
    logs_by_id: dict[str, Tuple[str, list[LogEntry]]],
    aggregator_class: Type[LogAggregator],
    config: Config,
    host: str,
) -> None:
    """
    Interactively traces mail hops starting from the given trace ID.

    Args:
        trace_id: The initial mail ID to trace.
        logs_by_id: Dictionary mapping mail IDs to lists of LogEntry objects.
        aggregator_class: The aggregator class to instantiate for each hop.
        config: The configuration object for aggregator instantiation.
        host: The current host.

    Returns:
        None
    """

    if trace_id not in logs_by_id:
        logger.info(f"Trace ID {trace_id} not found in logs")
        return

    aggregator = aggregator_class(host, config)

    while True:
        try:
            result = do_trace(trace_id, aggregator)
        except MailtraceError as e:
            handle_error(e)
            break
        except Exception as e:
            handle_error(e, exit_on_error=True)
            break

        if result is None:
            logger.info("No more hops")
            break
        print_blue(
            f"Relayed to {result.relay_host} ({result.relay_ip}:{result.relay_port}) with new ID {result.mail_id} (SMTP {result.smtp_code})"
        )
        trace_next_hop_ans: str = input(
            f"Trace next hop: {result.relay_host}? (Y/n/local/<next hop>): "
        ).lower()
        if trace_next_hop_ans in ["", "y"]:
            trace_id = result.mail_id
            try:
                aggregator = aggregator_class(result.relay_host, config)
            except MailtraceError as e:
                handle_error(e)
                break
        elif trace_next_hop_ans == "n":
            logger.info("Trace stopped")
            break
        elif trace_next_hop_ans == "local":
            trace_id = result.mail_id
            try:
                aggregator = aggregator_class(host, config)
            except MailtraceError as e:
                handle_error(e)
                break
        else:
            trace_id = result.mail_id
            try:
                aggregator = aggregator_class(trace_next_hop_ans, config)
            except MailtraceError as e:
                handle_error(e)
                break


@cli.command()
@click.option(
    "-c",
    "--config-path",
    "config_path",
    type=click.Path(exists=True),
    required=False,
    help="Path to configuration file",
)
@click.option(
    "-h",
    "--start-host",
    type=str,
    required=True,
    help="The starting host or cluster name",
)
@click.option(
    "-k",
    "--key",
    type=str,
    required=True,
    help="The keyword, can be email address, domain, etc.",
    multiple=True,
)
@click.option(
    "--login-pass", type=str, required=False, help="The login password"
)
@click.option(
    "--sudo-pass", type=str, required=False, help="The sudo password"
)
@click.option(
    "--opensearch-pass",
    type=str,
    required=False,
    help="The opensearch password",
)
@click.option("--ask-login-pass", is_flag=True, help="Ask for login password")
@click.option("--ask-sudo-pass", is_flag=True, help="Ask for sudo password")
@click.option(
    "--ask-opensearch-pass", is_flag=True, help="Ask for opensearch password"
)
@click.option("--time", type=str, required=True, help="The time")
@click.option(
    "--time-range",
    type=str,
    required=True,
    help="The time range, e.g. 1d, 10m",
)
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
):
    """
    Trace email messages through mail server logs.
    The entrypoint of this program.
    """

    try:
        config = load_config(config_path)
    except MailtraceError as e:
        handle_error(e, exit_on_error=True)
        return  # Won't reach here due to exit, but helps type checkers

    init_logger(config)

    handle_passwords(
        config,
        ask_login_pass,
        login_pass,
        ask_sudo_pass,
        sudo_pass,
        ask_opensearch_pass,
        opensearch_pass,
    )

    time_validation(time, time_range)

    logger.info("Running mailtrace...")

    try:
        aggregator_class = select_aggregator(config)
    except ConfigurationError as e:
        handle_error(e, exit_on_error=True)
        return

    hosts: list[str] = config.cluster_to_hosts(start_host) or [start_host]
    logger.info(f"Using hosts: {hosts}")
    logs_by_id: dict[str, tuple[str, list[LogEntry]]] = {}
    aggregator: LogAggregator | None = None

    for host in hosts:
        print(host)
        try:
            aggregator = aggregator_class(host, config)
            logs_by_id_from_host = query_and_print_logs(
                aggregator, key, time, time_range
            )
            logs_by_id.update(logs_by_id_from_host)
        except MailtraceError as e:
            handle_error(e)
            continue
        except Exception as e:
            handle_error(e)
            continue

    if aggregator is None:
        handle_error(
            ConfigurationError(
                "No aggregator could be created. All hosts failed.",
                "Check your connection settings and host availability",
            ),
            exit_on_error=True,
        )
        return

    if not logs_by_id:
        logger.warning("No logs found matching the query criteria on any host")
        logger.info(
            "Suggestion: Try adjusting your search keywords or time range"
        )
        return

    try:
        trace_id = input("Enter trace ID: ")
    except (KeyboardInterrupt, EOFError):
        logger.info("\nTrace cancelled by user")
        return

    if trace_id not in logs_by_id:
        logger.info(f"Trace ID {trace_id} not found in logs")
        return
    host_for_trace = logs_by_id[trace_id][0]
    trace_mail_loop(
        trace_id, logs_by_id, aggregator_class, config, host_for_trace
    )


if __name__ == "__main__":
    cli()
