import getpass
import logging

import click

from mailtrace.aggregator import do_trace, select_aggregator
from mailtrace.aggregator.base import LogAggregator
from mailtrace.config import Config, Method, load_config
from mailtrace.parser import LogEntry
from mailtrace.trace import query_logs_by_keywords, trace_mail_flow_to_file
from mailtrace.utils import print_blue, time_validation

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


def _prompt_password(
    prompt: str, ask: bool, provided: str | None
) -> str | None:
    """Prompt for password if asked, otherwise return provided value."""
    if ask:
        return getpass.getpass(prompt=prompt)
    return provided


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
    if config.method == Method.SSH:
        login_pass = _prompt_password(
            "Enter login password: ", ask_login_pass, login_pass
        )
        config.ssh_config.password = login_pass or config.ssh_config.password
        if not config.ssh_config.password:
            logger.warning(
                "Empty login password - no password will be used for login"
            )

        sudo_pass = _prompt_password(
            "Enter sudo password: ", ask_sudo_pass, sudo_pass
        )
        config.ssh_config.sudo_pass = sudo_pass or config.ssh_config.sudo_pass
        if not config.ssh_config.sudo_pass:
            logger.warning(
                "Empty sudo password - no password will be used for sudo"
            )

    elif config.method == Method.OPENSEARCH:
        opensearch_pass = _prompt_password(
            "Enter opensearch password: ", ask_opensearch_pass, opensearch_pass
        )
        config.opensearch_config.password = (
            opensearch_pass or config.opensearch_config.password
        )
        if not config.opensearch_config.password:
            logger.warning(
                "Empty opensearch password - no password will be used for opensearch"
            )
    else:
        logger.warning(
            f"Unknown method: {config.method}. No password handling."
        )


def print_logs_by_id(
    logs_by_id: dict[str, tuple[str, list[LogEntry]]],
) -> None:
    """
    Prints logs grouped by mail ID.

    Args:
        logs_by_id: Dictionary mapping mail IDs to (host, list of LogEntry) tuples.
    """
    for mail_id, (_, logs) in logs_by_id.items():
        print_blue(f"== Mail ID: {mail_id} ==")
        for log in logs:
            print(str(log))
        print_blue("==============\n")


def trace_mail_loop(
    trace_id: str,
    logs_by_id: dict[str, tuple[str, list[LogEntry]]],
    aggregator_class: type[LogAggregator],
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
    """
    if trace_id not in logs_by_id:
        logger.info(f"Trace ID {trace_id} not found in logs")
        return

    aggregator = aggregator_class(host, config)

    while True:
        result = do_trace(trace_id, aggregator)
        if result is None:
            logger.info("No more hops")
            break

        print_blue(
            f"Relayed to {result.relay_host} ({result.relay_ip}:{result.relay_port}) "
            f"with new ID {result.mail_id} (SMTP {result.smtp_code})"
        )

        # If auto_continue is enabled, automatically continue to the next hop
        if config.auto_continue:
            logger.info(
                f"Auto-continue enabled. Continuing to {result.relay_host}"
            )
            trace_next_hop_ans = "y"
        else:
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
            aggregator = aggregator_class(host, config)
        else:
            trace_id = result.mail_id
            aggregator = aggregator_class(trace_next_hop_ans, config)


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
