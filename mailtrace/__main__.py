import getpass

import click

from .aggregator import Opensearch, SSHHost, do_trace
from .config import Method, load_config
from .log import logger
from .models import LogQuery
from .parser import LogEntry
from .utils import time_validation


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
@click.option(
    "--opensearch-pass",
    type=str,
    required=False,
    help="The opensearch password",
)
# todo: ask login pass
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
    start_host,
    key,
    sudo_pass,
    opensearch_pass,
    ask_sudo_pass,
    ask_opensearch_pass,
    time,
    time_range,
):
    config = load_config()

    # log aggregator
    if config.method == Method.SSH:
        aggregator_class = SSHHost
    elif config.method == Method.OPENSEARCH:
        aggregator_class = Opensearch
    else:
        raise ValueError(f"Unsupported method: {config.method}")

    # sudo pass
    if ask_sudo_pass:
        sudo_pass = getpass.getpass(prompt="Enter sudo password: ")
    config.ssh_config.sudo_pass = sudo_pass or config.ssh_config.sudo_pass
    if not sudo_pass:
        logger.warning(
            "Warning: empty sudo password is provided, no password will be used for sudo"
        )

    # opensearch pass
    if ask_opensearch_pass:
        opensearch_pass = getpass.getpass(prompt="Enter opensearch password: ")
    config.opensearch_config.password = (
        opensearch_pass or config.opensearch_config.password
    )
    if not opensearch_pass:
        logger.warning(
            "Warning: empty opensearch password is provided, no password will be used for opensearch"
        )

    time_validation_results = time_validation(time, time_range)
    if time_validation_results:
        raise ValueError(time_validation_results)

    logger.info("Running mailtrace...")

    # Get and list all mail IDs with given query
    aggregator = aggregator_class(start_host, config)
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
        trace_next_hop_ans: str = input(
            f"Trace next hop: {next_hop}? (y/n/local): "
        ).lower()
        if trace_next_hop_ans == "y":
            trace_id = next_mail_id
            aggregator = aggregator_class(next_hop, config)
        elif trace_next_hop_ans == "local":
            trace_id = next_mail_id
            aggregator = aggregator_class(aggregator.host, config)
        else:
            logger.info("Trace stopped")
            break


if __name__ == "__main__":
    cli()
