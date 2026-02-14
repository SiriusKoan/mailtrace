import getpass
import logging

import click

from mailtrace.aggregator import (
    do_trace,
    do_trace_from_logs,
    extract_message_ids,
    select_aggregator,
)
from mailtrace.aggregator.base import LogAggregator
from mailtrace.config import Config, Method, load_config
from mailtrace.flow_check import check_cluster_flow
from mailtrace.models import LogQuery
from mailtrace.parser import LogEntry
from mailtrace.trace import trace_mail_flow_to_file
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
    click.option("--login-pass", type=str, required=False, help="The login password"),
    click.option("--sudo-pass", type=str, required=False, help="The sudo password"),
    click.option(
        "--opensearch-pass",
        type=str,
        required=False,
        help="The opensearch password",
    ),
    click.option("--ask-login-pass", is_flag=True, help="Ask for login password"),
    click.option("--ask-sudo-pass", is_flag=True, help="Ask for sudo password"),
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


def _prompt_password(prompt: str, ask: bool, provided: str | None) -> str | None:
    """Prompt for password if asked, otherwise return provided value."""
    if ask:
        return getpass.getpass(prompt=prompt)
    return provided


def _warn_cli_password(
    password: str | None, name: str, ask_flag: str, env_var: str
) -> None:
    """Log security warning if password was provided via CLI argument."""
    if password:
        logger.warning(
            "SECURITY: %s provided via CLI argument is visible in shell history. "
            "Consider using %s or %s env var instead.",
            name,
            ask_flag,
            env_var,
        )


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

    Security Note:
        Passwords passed via CLI arguments (--login-pass, --sudo-pass, --opensearch-pass)
        are visible in shell history and process listings. Use --ask-*-pass flags or
        environment variables for better security.
    """
    # SECURITY WARNING: CLI password arguments are visible in shell history and ps output
    _warn_cli_password(
        login_pass, "Password", "--ask-login-pass", "MAILTRACE_SSH_PASSWORD"
    )
    _warn_cli_password(
        sudo_pass,
        "Sudo password",
        "--ask-sudo-pass",
        "MAILTRACE_SUDO_PASSWORD",
    )
    _warn_cli_password(
        opensearch_pass,
        "OpenSearch password",
        "--ask-opensearch-pass",
        "MAILTRACE_OPENSEARCH_PASSWORD",
    )

    if config.method == Method.SSH:
        login_pass = _prompt_password(
            "Enter login password: ", ask_login_pass, login_pass
        )
        config.ssh_config.password = login_pass or config.ssh_config.password
        if not config.ssh_config.password:
            logger.warning("Empty login password - no password will be used for login")

        sudo_pass = _prompt_password("Enter sudo password: ", ask_sudo_pass, sudo_pass)
        config.ssh_config.sudo_pass = sudo_pass or config.ssh_config.sudo_pass
        if not config.ssh_config.sudo_pass:
            logger.warning("Empty sudo password - no password will be used for sudo")

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
        logger.warning(f"Unknown method: {config.method}. No password handling.")


def query_and_print_logs(
    aggregator: LogAggregator,
    key: list[str],
    time: str,
    time_range: str,
) -> dict[str, tuple[str, list[LogEntry]]]:
    """
    Queries logs using the aggregator and prints logs grouped by mail ID.

    Uses Message-ID based batch fetching to avoid N+1 queries:
    1. Keyword search (time-restricted) -> initial queue-IDs + Message-IDs
    2. Message-ID queries (no time restriction) -> all related queue-IDs
    3. Batch query all queue-IDs (no time restriction) -> all logs at once

    Args:
        aggregator: The aggregator instance to query logs.
        key: Keywords for the log query.
        time: Specific time for the log query.
        time_range: Time range for the log query.

    Returns:
        Dictionary mapping mail IDs to (host, list of LogEntry) tuples.
        Includes both initially-matched IDs and related IDs found via
        Message-ID for the trace cache.
    """
    # Step 1: keyword search (time-restricted)
    base_logs = aggregator.query_by(
        LogQuery(keywords=key, time=time, time_range=time_range)
    )
    initial_mail_ids = list(
        {log.mail_id for log in base_logs if log.mail_id is not None}
    )
    if not initial_mail_ids:
        logger.info("No mail IDs found")
        return {}

    # Step 2: find ALL related queue-IDs via Message-ID (no time restriction)
    message_ids = extract_message_ids(base_logs)
    all_queue_ids = set(initial_mail_ids)

    for mid in message_ids:
        logger.info("Querying by message_id: %s", mid)
        mid_logs = aggregator.query_by(LogQuery(message_id=mid))
        all_queue_ids.update(log.mail_id for log in mid_logs if log.mail_id)

    # Step 3: batch fetch ALL queue-IDs at once (no time restriction)
    logs_by_id: dict[str, tuple[str, list[LogEntry]]] = {}
    batch_logs = aggregator.query_by(LogQuery(mail_ids=list(all_queue_ids)))
    for entry in batch_logs:
        if entry.mail_id and entry.mail_id in all_queue_ids:
            logs_by_id.setdefault(entry.mail_id, (entry.hostname, []))[1].append(entry)

    # Step 4: fallback for any initial IDs not covered
    for mail_id in initial_mail_ids:
        if mail_id not in logs_by_id:
            logs = aggregator.query_by(LogQuery(mail_id=mail_id))
            logs_by_id[mail_id] = (aggregator.host, logs)

    # Step 5: print ONLY initially-matched queue-IDs
    for mail_id in initial_mail_ids:
        if mail_id in logs_by_id:
            _, logs = logs_by_id[mail_id]
            logs.sort(key=lambda e: e.datetime)
            print_blue(f"== Mail ID: {mail_id} ==")
            for log in logs:
                print(str(log))
            print_blue("==============\n")

    return logs_by_id


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

    while True:
        # Cache-first: use pre-fetched logs if available
        if trace_id in logs_by_id:
            _, cached_logs = logs_by_id[trace_id]
            result = do_trace_from_logs(trace_id, cached_logs)
        else:
            # Cache miss: query live
            aggregator = aggregator_class(host, config)
            result = do_trace(trace_id, aggregator)
            if result is None:
                # Retry without hostname filter (some machines lack proper reverse DNS)
                aggregator = aggregator_class("", config)
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
            logger.info(f"Auto-continue enabled. Continuing to {result.relay_host}")
            trace_next_hop_ans = "y"
        else:
            print(
                f"Trace next hop: {result.relay_host}? (Y/n/local/<next hop>): ",
                end="",
                flush=True,
            )
            trace_next_hop_ans: str = input().lower()

        logger.info("User selected: %r", trace_next_hop_ans)
        if trace_next_hop_ans in ["", "y"]:
            trace_id = result.mail_id
            host = result.relay_host
        elif trace_next_hop_ans == "n":
            logger.info("Trace stopped by user")
            break
        elif trace_next_hop_ans == "local":
            trace_id = result.mail_id
        else:
            trace_id = result.mail_id
            host = trace_next_hop_ans


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
    logs_by_id: dict[str, tuple[str, list[LogEntry]]] = {}
    if config.method == Method.OPENSEARCH:
        aggregator = aggregator_class(start_host, config)
        logs_by_id = query_and_print_logs(aggregator, key, time, time_range)
    elif config.method == Method.SSH:
        hosts: list[str] = config.cluster_to_hosts(start_host) or [start_host]
        logger.info(f"Using hosts: {hosts}")
        for host in hosts:
            print_blue(f"== Querying host: {host} ==")
            aggregator = aggregator_class(host, config)
            logs_by_id_from_host = query_and_print_logs(
                aggregator, key, time, time_range
            )
            logs_by_id.update(logs_by_id_from_host)

    if not logs_by_id:
        logger.info("No mail IDs found to trace.")
        return

    print("Enter trace ID: ", end="", flush=True)
    trace_id = input()
    logger.info("User selected trace ID: %s", trace_id)
    if trace_id not in logs_by_id:
        logger.info(f"Trace ID {trace_id} not found in logs")
        return

    host_for_trace = logs_by_id[trace_id][0]
    trace_mail_loop(trace_id, logs_by_id, aggregator_class, config, host_for_trace)


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


@cli.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    required=False,
    help="Path to configuration file (falls back to MAILTRACE_CONFIG env var)",
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"]),
    default="stdio",
    help="MCP transport type (default: stdio)",
)
@click.option(
    "--port",
    type=int,
    default=8080,
    help="Port for SSE transport (default: 8080)",
)
def mcp(config_path: str | None, transport: str, port: int) -> None:
    """Start the MCP server for LLM integration.

    The MCP server exposes mailtrace tools for use by LLM assistants
    like Claude.

    Examples:

        # Start with stdio transport (for Claude Code)
        mailtrace mcp --config /path/to/config.yaml

        # Start with SSE transport for remote access
        mailtrace mcp --config /path/to/config.yaml --transport sse --port 8080
    """
    from mailtrace.mcp import run_server

    config = load_config(config_path)
    configure_logging(config)

    logger.info(f"Starting MCP server with {transport} transport...")
    run_server(config, transport=transport, port=port)


@cli.command("flow-check")
@click.option(
    "-c",
    "--config-path",
    "config_path",
    type=click.Path(exists=True),
    required=False,
    help="Path to configuration file",
)
@click.option(
    "--cluster",
    type=str,
    required=True,
    help="Cluster name to check",
)
@click.option(
    "--time",
    "time_param",
    type=str,
    required=False,
    default=None,
    help="Reference time (YYYY-MM-DD HH:MM:SS). Default: now",
)
@click.option(
    "--time-range",
    type=str,
    default="1h",
    help="Time range (e.g., 1h, 30m). Default: 1h",
)
@click.option(
    "-k",
    "--key",
    type=str,
    required=False,
    help="Optional keyword filter",
    multiple=True,
)
@click.option("--login-pass", type=str, required=False)
@click.option("--sudo-pass", type=str, required=False)
@click.option("--opensearch-pass", type=str, required=False)
@click.option("--ask-login-pass", is_flag=True)
@click.option("--ask-sudo-pass", is_flag=True)
@click.option("--ask-opensearch-pass", is_flag=True)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    required=False,
    default=None,
    help="Output file for JSON result",
)
def flow_check(
    config_path,
    cluster,
    time_param,
    time_range,
    key,
    login_pass,
    sudo_pass,
    opensearch_pass,
    ask_login_pass,
    ask_sudo_pass,
    ask_opensearch_pass,
    output,
):
    """Check cluster flow conservation."""
    import datetime as dt
    import json

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

    check_time = time_param or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if time_param:
        err = time_validation(check_time, time_range)
        if err:
            raise ValueError(err)

    aggregator_class = select_aggregator(config)
    keywords = list(key) if key else None

    result = check_cluster_flow(
        config=config,
        aggregator_class=aggregator_class,
        cluster=cluster,
        time=check_time,
        time_range=time_range,
        keywords=keywords,
    )

    result_json = json.dumps(result.to_dict(), indent=2, default=str)
    if output and output != "-":
        with open(output, "w") as f:
            f.write(result_json)
    else:
        print(result_json)


if __name__ == "__main__":
    cli()
