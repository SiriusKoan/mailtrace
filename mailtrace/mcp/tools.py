"""MCP tool definitions for mailtrace."""

import json
import logging
from dataclasses import asdict
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mailtrace.aggregator import select_aggregator
from mailtrace.config import Config
from mailtrace.flow_check import check_cluster_flow
from mailtrace.trace import query_logs_by_keywords
from mailtrace.utils import time_validation
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

logger = logging.getLogger("mailtrace.mcp")


def _sanitize_error_message(error: Exception) -> str:
    """Sanitize error message to avoid leaking sensitive information.

    This function removes potentially sensitive information like hostnames,
    usernames, file paths, and connection details from error messages before
    returning them to clients.

    Args:
        error: The exception to sanitize

    Returns:
        A sanitized error message suitable for external clients
    """
    error_type = type(error).__name__
    error_msg = str(error)

    # Map common error types to generic messages
    sensitive_patterns = {
        "AuthenticationException": "Authentication failed. Check credentials.",
        "SSHException": "SSH connection failed. Check host and credentials.",
        "NoValidConnectionsError": "Unable to connect to the host.",
        "socket.timeout": "Connection timed out.",
        "ConnectionRefusedError": "Connection refused by the host.",
        "HostKeyVerificationError": "SSH host key verification failed.",
        "ConnectionError": "Failed to establish connection.",
        "TimeoutError": "Operation timed out.",
    }

    for pattern, generic_msg in sensitive_patterns.items():
        if pattern in error_type or pattern in error_msg:
            return generic_msg

    # For unknown errors, return type without full message (may contain sensitive data)
    # Log the full error server-side for debugging
    logger.debug(f"Full error details: {error_type}: {error_msg}")
    return f"Operation failed ({error_type}). Check server logs for details."


def _error_response(code: str, message: str) -> str:
    """Create a JSON error response."""
    return json.dumps({"error": {"code": code, "message": message}})


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
    message_id: Optional[str] = Field(
        default=None,
        description="RFC 2822 Message-ID header to trace across all hops",
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
        """Validate that either mail_id, message_id, or keywords is provided."""
        if not self.mail_id and not self.message_id and not self.keywords:
            raise ValueError(
                "Either 'mail_id', 'message_id', or 'keywords' must be provided"
            )


class CheckFlowInput(BaseModel):
    """Input model for mailtrace_check_flow tool."""

    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True
    )

    cluster: str = Field(
        ...,
        description="Cluster name to check (must be in config)",
        min_length=1,
    )
    time: Optional[str] = Field(
        default=None,
        description="Reference time (YYYY-MM-DD HH:MM:SS). Defaults to now.",
    )
    time_range: str = Field(
        default="1h",
        description="Time range (e.g., '1h', '30m'). Default: 1h",
    )
    keywords: Optional[list[str]] = Field(
        default=None,
        description="Optional keyword filter (email/domain)",
    )


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
                    return _error_response("INVALID_TIME_FORMAT", error)

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
                return _error_response(
                    "NO_RESULTS",
                    f"No mail logs found for keywords: {params.keywords}. "
                    "Try widening the time range or checking different keywords.",
                )

            # Format response
            entries = []
            for mail_id, (host, log_entries) in logs_by_id.items():
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
            return _error_response("CONFIG_ERROR", str(e))
        except Exception as e:
            logger.exception("Error in mailtrace_query_logs")
            return _error_response(
                "CONNECTION_FAILED", _sanitize_error_message(e)
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
        - message_id: Trace by RFC 2822 Message-ID across all hops
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
        from mailtrace.trace import (
            trace_mail_flow,
            trace_mail_flow_by_message_id,
        )

        try:
            # Validate time parameters if provided
            if params.time and params.time_range:
                error = time_validation(params.time, params.time_range)
                if error:
                    return _error_response("INVALID_TIME_FORMAT", error)

            # Get aggregator class
            aggregator_class = select_aggregator(config)

            graph = MailGraph()
            traced_ids = []
            search_mode = "keywords"

            if params.message_id:
                # Direct message_id batch trace
                search_mode = "message_id"
                aggregator = aggregator_class(params.host, config)
                trace_mail_flow_by_message_id(
                    params.message_id, aggregator, graph
                )
                traced_ids.append(params.message_id)
            elif params.mail_id:
                # Direct mail_id provided
                search_mode = "mail_id"
                trace_mail_flow(
                    trace_id=params.mail_id,
                    aggregator_class=aggregator_class,
                    config=config,
                    host=params.host,
                    graph=graph,
                )
                traced_ids.append(params.mail_id)
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
                    return _error_response(
                        "NO_RESULTS",
                        f"No mail logs found for keywords: {params.keywords}. "
                        "Try widening the time range or checking different keywords.",
                    )

                for mail_id, (host, _) in logs_by_id.items():
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
                "search_mode": search_mode,
            }
            if params.keywords:
                result["trace"]["keywords"] = params.keywords
            if params.message_id:
                result["trace"]["message_id"] = params.message_id

            if not result["edges"]:
                if params.message_id:
                    search_target = f"message ID: {params.message_id}"
                elif params.mail_id:
                    search_target = f"mail ID: {params.mail_id}"
                else:
                    search_target = f"keywords: {params.keywords}"
                return _error_response(
                    "NO_RESULTS",
                    f"No relay hops found for {search_target}. "
                    "The mail may have been delivered locally "
                    "or the search criteria may be incorrect.",
                )

            return json.dumps(result, indent=2, default=str)

        except FileNotFoundError as e:
            return _error_response("CONFIG_ERROR", str(e))
        except Exception as e:
            logger.exception("Error in mailtrace_trace_mail")
            return _error_response(
                "CONNECTION_FAILED", _sanitize_error_message(e)
            )

    @mcp.tool(
        name="mailtrace_check_flow",
        annotations=ToolAnnotations(
            title="Check Cluster Flow Conservation",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def mailtrace_check_flow(
        params: CheckFlowInput,
    ) -> str:
        """Check that all inbound mail to a cluster reached
        a terminal state.

        Args:
            params: Check parameters including cluster, time,
                time_range.

        Returns:
            JSON with summary, complete/problematic flows,
            out-of-window mail IDs.
        """
        try:
            import datetime as dt

            check_time = params.time or dt.datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            if params.time:
                error = time_validation(check_time, params.time_range)
                if error:
                    return _error_response("INVALID_TIME_FORMAT", error)

            aggregator_class = select_aggregator(config)
            result = check_cluster_flow(
                config=config,
                aggregator_class=aggregator_class,
                cluster=params.cluster,
                time=check_time,
                time_range=params.time_range,
                keywords=params.keywords,
            )

            return json.dumps(result.to_dict(), indent=2, default=str)

        except ValueError as e:
            return _error_response("INVALID_CLUSTER", str(e))
        except FileNotFoundError as e:
            return _error_response("CONFIG_ERROR", str(e))
        except Exception as e:
            logger.exception("Error in mailtrace_check_flow")
            return _error_response(
                "CONNECTION_FAILED", _sanitize_error_message(e)
            )
