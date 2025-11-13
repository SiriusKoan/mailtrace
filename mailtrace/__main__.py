import click

from mailtrace.config import Method, load_config
from mailtrace.graph import MailGraph
from mailtrace.log import init_logger, logger
from mailtrace.models import LogEntry
from mailtrace.utils import time_validation
from mailtrace.common import (
    handle_passwords,
    query_and_print_logs,
    select_aggregator,
    trace_mail_flow,
    trace_mail_loop,
)


@click.group()
def cli():
    pass


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
@click.option("--login-pass", type=str, required=False, help="The login password")
@click.option("--sudo-pass", type=str, required=False, help="The sudo password")
@click.option(
    "--opensearch-pass",
    type=str,
    required=False,
    help="The opensearch password",
)
@click.option("--ask-login-pass", is_flag=True, help="Ask for login password")
@click.option("--ask-sudo-pass", is_flag=True, help="Ask for sudo password")
@click.option("--ask-opensearch-pass", is_flag=True, help="Ask for opensearch password")
@click.option("--time", type=str, required=True, help="The time")
@click.option(
    "--time-range",
    type=str,
    required=True,
    help="The time range, e.g. 1d, 10m",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    required=True,
    help="Output file for the Graphviz dot graph",
)
def trace(
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
    output: str,
):
    """
    Trace email messages and generate a Graphviz dot file.
    """
    config = load_config(config_path)
    # init_logger(config)
    handle_passwords(
        config,
        ask_login_pass,
        login_pass,
        ask_sudo_pass,
        sudo_pass,
        ask_opensearch_pass,
        opensearch_pass,
    )
    time_validation_results = time_validation(time, time_range)
    if time_validation_results:
        raise ValueError(time_validation_results)

    logger.info("Running mailtrace...")
    aggregator_class = select_aggregator(config)
    logs_by_id: dict[str, tuple[str, list[LogEntry]]] = {}
    if config.method == Method.OPENSEARCH:
        aggregator = aggregator_class(start_host, config)
        logs_by_id = query_and_print_logs(aggregator, key, time, time_range)
    elif config.method == Method.SSH:
        hosts: list[str] = config.cluster_to_hosts(start_host) or [start_host]
        logger.info(f"Using hosts: {hosts}")
        for host in hosts:
            aggregator = aggregator_class(host, config)
            logs_by_id_from_host = query_and_print_logs(
                aggregator, key, time, time_range
            )
            logs_by_id.update(logs_by_id_from_host)

    if not logs_by_id:
        logger.info("No mail IDs found to trace.")
        return

    graph = MailGraph()
    for trace_id, (host_for_trace, _) in logs_by_id.items():
        trace_mail_flow(trace_id, aggregator_class, config, host_for_trace, graph)

    graph.to_dot(output)
    logger.info(f"Graph saved to {output}")


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
@click.option("--login-pass", type=str, required=False, help="The login password")
@click.option("--sudo-pass", type=str, required=False, help="The sudo password")
@click.option(
    "--opensearch-pass",
    type=str,
    required=False,
    help="The opensearch password",
)
@click.option("--ask-login-pass", is_flag=True, help="Ask for login password")
@click.option("--ask-sudo-pass", is_flag=True, help="Ask for sudo password")
@click.option("--ask-opensearch-pass", is_flag=True, help="Ask for opensearch password")
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
    The entrypoiny of this program.
    """

    config = load_config(config_path)
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
    time_validation_results = time_validation(time, time_range)
    if time_validation_results:
        raise ValueError(time_validation_results)

    logger.info("Running mailtrace...")
    aggregator_class = select_aggregator(config)
    logs_by_id: dict[str, tuple[str, list[LogEntry]]] = {}
    if config.method == Method.OPENSEARCH:
        aggregator = aggregator_class(start_host, config)
        logs_by_id = query_and_print_logs(aggregator, key, time, time_range)
    elif config.method == Method.SSH:
        hosts: list[str] = config.cluster_to_hosts(start_host) or [start_host]
        logger.info(f"Using hosts: {hosts}")
        for host in hosts:
            print(host)
            aggregator = aggregator_class(host, config)
            logs_by_id_from_host = query_and_print_logs(
                aggregator, key, time, time_range
            )
            logs_by_id.update(logs_by_id_from_host)

    if not logs_by_id:
        logger.info("No mail IDs found to trace.")
        return

    trace_id = input("Enter trace ID: ")
    if trace_id not in logs_by_id:
        logger.info(f"Trace ID {trace_id} not found in logs")
        return
    host_for_trace = logs_by_id[trace_id][0]
    trace_mail_loop(trace_id, logs_by_id, aggregator_class, config, host_for_trace)


if __name__ == "__main__":
    from mailtrace.mcp import mcp_cli

    cli.add_command(mcp_cli)
    cli()
