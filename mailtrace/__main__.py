import getpass
import re

import click

from .aggregator import SSHHost, do_trace
from .config import Method, load_config
from .log import logger
from .models import LogQuery
from .parser import LogEntry


@click.group()
def cli():
    pass


@cli.command()
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
    "-K", "--sudo-pass", type=str, required=False, help="The sudo password"
)
@click.option("--ask-sudo-pass", is_flag=True, help="Ask for sudo password")
@click.option("--time", type=str, required=False, help="The time")
@click.option(
    "--time-range",
    type=str,
    required=False,
    help="The time range, e.g. 1d, 10m",
)
def run(start_host, key, sudo_pass, ask_sudo_pass, time, time_range):
    config = load_config()
    if config.method != Method.SSH:
        raise ValueError("Unsupported method")
    if ask_sudo_pass:
        sudo_pass = getpass.getpass(prompt="Enter sudo password: ")
    if time:
        time_pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        if not time_pattern.match(time):
            raise ValueError(
                f"Time {time} should be in format YYYY-MM-DD HH:MM:SS"
            )
    if time and not time_range or time_range and not time:
        raise ValueError("Time and time-range must be provided together")
    time_range_pattern = re.compile(r"^\d+[dhm]$")
    if time_range and not time_range_pattern.match(time_range):
        raise ValueError("time_range should be in format [0-9]+[dhm]")
    config.ssh_config.sudo_pass = sudo_pass or config.ssh_config.sudo_pass
    if not sudo_pass:
        logger.warning(
            "Warning: empty sudo password is provided, no password will be used for sudo"
        )

    logger.info("Running mailtrace...")

    # Get and list all mail IDs with given query
    aggregator = SSHHost(start_host, config)
    base_logs = aggregator.query_by(
        LogQuery(keywords=key, time=time, time_range=time_range)
    )
    ids = list({log.mail_id for log in base_logs if log.mail_id is not None})
    if not ids:
        logger.info("No mail IDs found")
        return
    logs_by_id: dict[str, list[LogEntry]] = {}
    for mail_id in ids:
        logs_by_id[mail_id] = aggregator.query_by(LogQuery(mail_id=mail_id))
        print(f"== Mail ID: {mail_id} ==")
        for log in logs_by_id[mail_id]:
            print(str(log))
        print("==============\n")

    # Get the wanted mail ID
    trace_id = input("Enter trace ID: ")
    if trace_id not in logs_by_id:
        logger.info(f"Trace ID {trace_id} not found in logs")
        return

    # Trace the mail
    while True:
        next_mail_id, next_hop = do_trace(trace_id, aggregator)
        if next_hop == "":
            logger.info("No more hops")
            break
        trace_next_hop: bool = (
            input(f"Trace next hop: {next_hop}? (y/n): ")
            .lower()
            .startswith("y")
        )
        if trace_next_hop:
            trace_id = next_mail_id
            aggregator = SSHHost(next_hop, config)
        else:
            logger.info("Trace stopped")
            break


if __name__ == "__main__":
    cli()
