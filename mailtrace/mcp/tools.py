"""MCP tool definitions for mailtrace."""

import json
import logging
from dataclasses import asdict
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mailtrace.aggregator import select_aggregator
from mailtrace.config import Config
from mailtrace.trace import query_logs_by_keywords
from mailtrace.utils import time_validation

logger = logging.getLogger("mailtrace.mcp")


class QueryLogsInput(BaseModel):
    """Input model for mailtrace_query_logs tool."""

    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True
    )

    host: str = Field(
        ...,
        description="Mail server hostname to query (must be defined in config)",
        min_length=1,
    )
    keywords: list[str] = Field(
        ...,
        description="Email addresses or domains to search for",
        min_length=1,
    )
    time: Optional[str] = Field(
        default=None,
        description="Reference time for search (format: YYYY-MM-DD HH:MM:SS)",
    )
    time_range: Optional[str] = Field(
        default=None,
        description="Time range around reference time (e.g., '10h', '30m', '1d')",
    )

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, v: list[str]) -> list[str]:
        if not v or all(not k.strip() for k in v):
            raise ValueError("At least one non-empty keyword required")
        return [k.strip() for k in v if k.strip()]


class TraceMailInput(BaseModel):
    """Input model for mailtrace_trace_mail tool."""

    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True
    )

    host: str = Field(
        ...,
        description="Starting mail server hostname",
        min_length=1,
    )
    mail_id: Optional[str] = Field(
        default=None,
        description="Mail queue ID to trace directly",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Email addresses or domains to search for (finds mail IDs automatically)",
    )
    time: Optional[str] = Field(
        default=None,
        description="Reference time for log search (format: YYYY-MM-DD HH:MM:SS)",
    )
    time_range: Optional[str] = Field(
        default=None,
        description="Time range for log search (e.g., '10h', '30m', '1d')",
    )

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, v: list[str]) -> list[str]:
        return [k.strip() for k in v if k.strip()]

    def model_post_init(self, __context) -> None:
        """Validate that either mail_id or keywords is provided."""
        if not self.mail_id and not self.keywords:
            raise ValueError("Either 'mail_id' or 'keywords' must be provided")


def register_resources(mcp: FastMCP, config: Config) -> None:
    """Register MCP resources for mailtrace."""

    @mcp.resource("mailtrace://clusters")
    def get_available_clusters() -> str:
        """List all available clusters that can be queried.

        Returns a JSON object mapping cluster names to their member hosts.
        """
        return json.dumps(config.clusters or {}, indent=2)


def register_tools(mcp: FastMCP, config: Config) -> None:
    """Register all mailtrace tools with the MCP server."""

    @mcp.tool(
        name="mailtrace_query_logs",
        annotations=ToolAnnotations(
            title="Query Mail Logs",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def mailtrace_query_logs(params: QueryLogsInput) -> str:
        """Search mail logs by email address or domain within a time range.

        Returns matching log entries that can be used to find mail IDs
        for tracing. Use this tool first to discover mail IDs, then use
        mailtrace_trace_mail to follow the mail flow.

        Args:
            params: Query parameters including host, keywords, time,
                and time_range.

        Returns:
            JSON containing log entries grouped by mail ID, with count
            and query info.
        """
        try:
            # Validate time parameters if provided
            if params.time and params.time_range:
                error = time_validation(params.time, params.time_range)
                if error:
                    return json.dumps(
                        {
                            "error": {
                                "code": "INVALID_TIME_FORMAT",
                                "message": error,
                            }
                        }
                    )

            # Get aggregator class
            aggregator_class = select_aggregator(config)

            # Query logs
            logs_by_id = query_logs_by_keywords(
                config=config,
                aggregator_class=aggregator_class,
                start_host=params.host,
                keywords=params.keywords,
                time=params.time or "",
                time_range=params.time_range or "",
            )

            if not logs_by_id:
                return json.dumps(
                    {
                        "error": {
                            "code": "NO_RESULTS",
                            "message": (
                                f"No mail logs found for keywords: "
                                f"{params.keywords}. Try widening the time "
                                "range or checking different keywords."
                            ),
                        }
                    }
                )

            # Format response
            entries = []
            for _host, log_entries in logs_by_id.values():
                for entry in log_entries:
                    entries.append(asdict(entry))

            response = {
                "entries": entries,
                "count": len(entries),
                "mail_ids": list(logs_by_id.keys()),
                "query": {
                    "host": params.host,
                    "keywords": params.keywords,
                    "time": params.time,
                    "time_range": params.time_range,
                },
            }
            return json.dumps(response, indent=2, default=str)

        except FileNotFoundError as e:
            return json.dumps(
                {"error": {"code": "CONFIG_ERROR", "message": str(e)}}
            )
        except Exception as e:
            logger.exception("Error in mailtrace_query_logs")
            return json.dumps(
                {
                    "error": {
                        "code": "CONNECTION_FAILED",
                        "message": f"Failed to query logs: {e}",
                    }
                }
            )

    @mcp.tool(
        name="mailtrace_trace_mail",
        annotations=ToolAnnotations(
            title="Trace Mail Flow",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def mailtrace_trace_mail(params: TraceMailInput) -> str:
        """Trace mail through the relay chain until delivery or failure.

        Can trace by:
        - mail_id: Trace a specific mail queue ID directly
        - keywords: Search for emails by address/domain, then trace all found

        Returns the complete mail flow as a Graphviz DOT graph plus
        structured edge data.

        Args:
            params: Trace parameters including host, mail_id or keywords,
                time, and time_range.

        Returns:
            JSON containing graph_dot (Graphviz DOT format), nodes,
            edges, hop_count, and traced mail IDs.
        """
        from mailtrace.graph import MailGraph
        from mailtrace.trace import trace_mail_flow

        try:
            # Validate time parameters if provided
            if params.time and params.time_range:
                error = time_validation(params.time, params.time_range)
                if error:
                    return json.dumps(
                        {
                            "error": {
                                "code": "INVALID_TIME_FORMAT",
                                "message": error,
                            }
                        }
                    )

            # Get aggregator class
            aggregator_class = select_aggregator(config)

            # Determine mail IDs to trace
            mail_ids_to_trace: list[tuple[str, str]] = []  # (mail_id, host)

            if params.mail_id:
                # Direct mail_id provided
                mail_ids_to_trace.append((params.mail_id, params.host))
            else:
                # Search by keywords first
                logs_by_id = query_logs_by_keywords(
                    config=config,
                    aggregator_class=aggregator_class,
                    start_host=params.host,
                    keywords=params.keywords or [],
                    time=params.time or "",
                    time_range=params.time_range or "",
                )

                if not logs_by_id:
                    return json.dumps(
                        {
                            "error": {
                                "code": "NO_RESULTS",
                                "message": (
                                    f"No mail logs found for keywords: "
                                    f"{params.keywords}. Try widening the time "
                                    "range or checking different keywords."
                                ),
                            }
                        }
                    )

                for mail_id, (host, _) in logs_by_id.items():
                    mail_ids_to_trace.append((mail_id, host))

            # Create graph and trace all mail IDs
            graph = MailGraph()
            traced_ids = []
            for mail_id, host in mail_ids_to_trace:
                trace_mail_flow(
                    trace_id=mail_id,
                    aggregator_class=aggregator_class,
                    config=config,
                    host=host,
                    graph=graph,
                )
                traced_ids.append(mail_id)

            # Get graph data
            result = graph.to_dict()
            result["trace"] = {
                "start_host": params.host,
                "mail_ids": traced_ids,
                "search_mode": "mail_id" if params.mail_id else "keywords",
            }
            if params.keywords:
                result["trace"]["keywords"] = params.keywords

            if not result["edges"]:
                search_target = (
                    f"mail ID: {params.mail_id}"
                    if params.mail_id
                    else f"keywords: {params.keywords}"
                )
                return json.dumps(
                    {
                        "error": {
                            "code": "NO_RESULTS",
                            "message": (
                                f"No relay hops found for {search_target}. "
                                "The mail may have been delivered locally "
                                "or the search criteria may be incorrect."
                            ),
                        }
                    }
                )

            return json.dumps(result, indent=2, default=str)

        except FileNotFoundError as e:
            return json.dumps(
                {"error": {"code": "CONFIG_ERROR", "message": str(e)}}
            )
        except Exception as e:
            logger.exception("Error in mailtrace_trace_mail")
            return json.dumps(
                {
                    "error": {
                        "code": "CONNECTION_FAILED",
                        "message": f"Failed to trace mail: {e}",
                    }
                }
            )
