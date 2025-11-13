import asyncio
from typing import Type
from uuid import uuid4

import click
from mcp.server.fastmcp import FastMCP

from mailtrace.aggregator.base import LogAggregator
from mailtrace.config import Config, load_config
from mailtrace.graph import MailGraph
from mailtrace.log import init_logger, logger
from mailtrace.common import (
    handle_passwords,
    query_and_print_logs,
    select_aggregator,
    trace_mail_flow,
)

scans = {}

mcp = FastMCP("mailtrace")


@mcp.tool()
def scan(
    start_host: str,
    key: str,
    time: str,
    time_range: str,
    config_path: str | None = None,
    login_pass: str | None = None,
    sudo_pass: str | None = None,
    opensearch_pass: str | None = None,
) -> str:
    """
    Triggers a mail trace scan.

    Args:
        start_host: The starting host or cluster name.
        key: The keyword, can be email address, domain, etc.
        time: The time for the trace.
        time_range: The time range for the trace.
        config_path: Path to configuration file.
        login_pass: The login password.
        sudo_pass: The sudo password.
        opensearch_pass: The opensearch password.

    Returns:
        The scan ID.
    """
    scan_id = str(uuid4())
    scans[scan_id] = {"status": "running", "graph": None}
    asyncio.create_task(
        run_scan(
            scan_id,
            start_host,
            [key],
            time,
            time_range,
            config_path,
            login_pass,
            sudo_pass,
            opensearch_pass,
        )
    )
    return scan_id


@mcp.resource("scan://{scan_id}")
def get_scan(scan_id: str) -> dict:
    """
    Retrieves the status and result of a scan.

    Args:
        scan_id: The ID of the scan.

    Returns:
        A dictionary with the scan status and the graph.
    """
    logger.debug(f"Retrieving scan {scan_id}", scans)
    return scans.get(scan_id, {"status": "not_found"})


async def run_scan(
    scan_id: str,
    start_host: str,
    key: list[str],
    time: str,
    time_range: str,
    config_path: str | None,
    login_pass: str | None,
    sudo_pass: str | None,
    opensearch_pass: str | None,
):
    try:
        config = load_config(config_path)
        init_logger(config)
        handle_passwords(
            config,
            False,
            login_pass,
            False,
            sudo_pass,
            False,
            opensearch_pass,
        )

        aggregator_class = select_aggregator(config)
        aggregator = aggregator_class(start_host, config)
        logs_by_id = query_and_print_logs(aggregator, key, time, time_range)

        if not logs_by_id:
            scans[scan_id]["status"] = "completed"
            return

        graph = MailGraph()
        for trace_id, (host_for_trace, _) in logs_by_id.items():
            trace_mail_flow(trace_id, aggregator_class, config, host_for_trace, graph)

        import io
        from networkx.drawing import nx_pydot

        dot_graph = io.StringIO()
        nx_pydot.write_dot(graph.graph, dot_graph)
        scans[scan_id]["graph"] = dot_graph.getvalue()
        scans[scan_id]["status"] = "completed"

    except Exception as e:
        logger.error(f"Scan {scan_id} failed: {e}")
        scans[scan_id]["status"] = "failed"


@click.group("mcp")
def mcp_cli():
    """Master Control Program for mailtrace."""
    pass


@mcp_cli.command()
def serve(host: str, port: int):
    """Runs the MCP server."""
    mcp.run()
