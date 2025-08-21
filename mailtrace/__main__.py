import getpass

import click

from .aggregator import OpenSearch, SSHHost, do_trace
from .config import Method, load_config
from .log import init_logger, logger
from .models import LogQuery
from .parser import LogEntry
from .utils import print_blue, time_validation


@click.group()
def cli():
    pass


def handle_passwords(
    config,
    ask_login_pass,
    login_pass,
    ask_sudo_pass,
    sudo_pass,
    ask_opensearch_pass,
    opensearch_pass,
):
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


def select_aggregator(config):
    """
    Selects and returns the appropriate log aggregator class based on the config method.

    Args:
        config: The configuration object containing the method attribute.

    Returns:
        The aggregator class (SSHHost or OpenSearch).

    Raises:
        ValueError: If the method is unsupported.
    """

    if config.method == Method.SSH:
        return SSHHost
    elif config.method == Method.OPENSEARCH:
        return OpenSearch
    else:
        raise ValueError(f"Unsupported method: {config.method}")


def query_and_print_logs(aggregator, key, time, time_range):
    """
    Queries logs using the aggregator and prints logs grouped by mail ID.

    Args:
        aggregator: The aggregator instance to query logs.
        key: Keywords for the log query.
        time: Specific time for the log query.
        time_range: Time range for the log query.

    Returns:
        logs_by_id: Dictionary mapping mail IDs to lists of LogEntry objects.
        ids: List of mail IDs found.
    """

    base_logs = aggregator.query_by(
        LogQuery(keywords=key, time=time, time_range=time_range)
    )
    ids = list({log.mail_id for log in base_logs if log.mail_id is not None})
    if not ids:
        logger.info("No mail IDs found")
        return {}, ids
    logs_by_id: dict[str, list[LogEntry]] = {}
    for mail_id in ids:
        logs_by_id[mail_id] = aggregator.query_by(LogQuery(mail_id=mail_id))
        print_blue(f"== Mail ID: {mail_id} ==")
        for log in logs_by_id[mail_id]:
            print(str(log))
        print_blue("==============\n")
    return logs_by_id, ids


def trace_mail_loop(
    trace_id, logs_by_id, aggregator_class, config, aggregator
):
    """
    Interactively traces mail hops starting from the given trace ID.

    Args:
        trace_id: The initial mail ID to trace.
        logs_by_id: Dictionary mapping mail IDs to lists of LogEntry objects.
        aggregator_class: The aggregator class to instantiate for each hop.
        config: The configuration object for aggregator instantiation.
        aggregator: The current aggregator instance.

    Returns:
        None
    """

    if trace_id not in logs_by_id:
        logger.info(f"Trace ID {trace_id} not found in logs")
        return

    while True:
        result = do_trace(trace_id, aggregator)
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
            aggregator = aggregator_class(result.relay_host, config)
        elif trace_next_hop_ans == "n":
            logger.info("Trace stopped")
            break
        elif trace_next_hop_ans == "local":
            trace_id = result.mail_id
            aggregator = aggregator_class(aggregator.host, config)
        else:
            trace_id = result.mail_id
            aggregator = aggregator_class(trace_next_hop_ans, config)


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
    "-h", "--start-host", type=str, required=True, help="The starting host"
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
@click.option("--time", type=str, required=False, help="The time")
@click.option(
    "--time-range",
    type=str,
    required=False,
    help="The time range, e.g. 1d, 10m",
)
def run(
    config_path,
    start_host,
    key,
    login_pass,
    sudo_pass,
    opensearch_pass,
    ask_login_pass,
    ask_sudo_pass,
    ask_opensearch_pass,
    time,
    time_range,
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
    aggregator = aggregator_class(start_host, config)
    logs_by_id, ids = query_and_print_logs(aggregator, key, time, time_range)
    if not ids:
        return
    trace_id = input("Enter trace ID: ")
    trace_mail_loop(trace_id, logs_by_id, aggregator_class, config, aggregator)


if __name__ == "__main__":
    cli()
