"""Tests for MCP tools with mocked aggregators."""

import json
from unittest.mock import MagicMock, patch

import pytest

from mailtrace.config import Config, Method
from mailtrace.mcp.tools import QueryLogsInput, TraceMailInput, register_tools
from mailtrace.models import LogEntry
from mcp.server.fastmcp import FastMCP


@pytest.fixture
def mock_config():
    """Create a mock Config object."""
    config = MagicMock(spec=Config)
    config.method = Method.SSH
    return config


@pytest.fixture
def sample_log_entry():
    """Create a sample LogEntry for testing."""
    return LogEntry(
        datetime="2025-01-15T10:00:00+08:00",
        hostname="mail.example.com",
        service="postfix/smtp",
        mail_id="ABC123DEF",
        message="to=<user@example.com>, relay=mail2.example.com[192.168.1.2]:25, status=sent (250 2.0.0 Ok: queued as XYZ789)",
    )


@pytest.fixture
def sample_logs_by_id(sample_log_entry):
    """Create sample logs_by_id dictionary."""
    return {
        "ABC123DEF": ("mail.example.com", [sample_log_entry]),
    }


class TestMailtraceQueryLogsTool:
    """Tests for mailtrace_query_logs MCP tool."""

    @pytest.mark.asyncio
    async def test_query_logs_success(self, mock_config, sample_logs_by_id):
        """Test successful query logs returns JSON with entries."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        # Get the registered tool
        tools = mcp._tool_manager._tools
        query_logs_tool = tools.get("mailtrace_query_logs")
        assert query_logs_tool is not None

        # Mock query_logs_by_keywords
        with (
            patch("mailtrace.mcp.tools.query_logs_by_keywords") as mock_query,
            patch("mailtrace.mcp.tools.select_aggregator") as mock_select,
        ):
            mock_select.return_value = MagicMock()
            mock_query.return_value = sample_logs_by_id

            params = QueryLogsInput(
                host="mail.example.com",
                keywords=["user@example.com"],
            )

            result = await query_logs_tool.fn(params)
            data = json.loads(result)

            assert "error" not in data
            assert data["count"] == 1
            assert "ABC123DEF" in data["mail_ids"]
            assert len(data["entries"]) == 1
            assert data["entries"][0]["mail_id"] == "ABC123DEF"

    @pytest.mark.asyncio
    async def test_query_logs_no_results(self, mock_config):
        """Test query logs returns error when no results found."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        query_logs_tool = tools.get("mailtrace_query_logs")

        with (
            patch("mailtrace.mcp.tools.query_logs_by_keywords") as mock_query,
            patch("mailtrace.mcp.tools.select_aggregator") as mock_select,
        ):
            mock_select.return_value = MagicMock()
            mock_query.return_value = {}

            params = QueryLogsInput(
                host="mail.example.com",
                keywords=["nonexistent@example.com"],
            )

            result = await query_logs_tool.fn(params)
            data = json.loads(result)

            assert "error" in data
            assert data["error"]["code"] == "NO_RESULTS"

    @pytest.mark.asyncio
    async def test_query_logs_invalid_time_format(self, mock_config):
        """Test query logs returns error for invalid time format."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        query_logs_tool = tools.get("mailtrace_query_logs")

        with patch("mailtrace.mcp.tools.time_validation") as mock_time:
            mock_time.return_value = "Invalid time format"

            params = QueryLogsInput(
                host="mail.example.com",
                keywords=["user@example.com"],
                time="invalid-time",
                time_range="10h",
            )

            result = await query_logs_tool.fn(params)
            data = json.loads(result)

            assert "error" in data
            assert data["error"]["code"] == "INVALID_TIME_FORMAT"

    @pytest.mark.asyncio
    async def test_query_logs_config_error(self, mock_config):
        """Test query logs handles FileNotFoundError as config error."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        query_logs_tool = tools.get("mailtrace_query_logs")

        with patch("mailtrace.mcp.tools.select_aggregator") as mock_select:
            mock_select.side_effect = FileNotFoundError("Config not found")

            params = QueryLogsInput(
                host="mail.example.com",
                keywords=["user@example.com"],
            )

            result = await query_logs_tool.fn(params)
            data = json.loads(result)

            assert "error" in data
            assert data["error"]["code"] == "CONFIG_ERROR"

    @pytest.mark.asyncio
    async def test_query_logs_connection_failed(self, mock_config):
        """Test query logs handles generic exceptions as connection failure."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        query_logs_tool = tools.get("mailtrace_query_logs")

        with patch("mailtrace.mcp.tools.select_aggregator") as mock_select:
            mock_select.side_effect = RuntimeError("Connection refused")

            params = QueryLogsInput(
                host="mail.example.com",
                keywords=["user@example.com"],
            )

            result = await query_logs_tool.fn(params)
            data = json.loads(result)

            assert "error" in data
            assert data["error"]["code"] == "CONNECTION_FAILED"


class TestMailtraceTraceMailTool:
    """Tests for mailtrace_trace_mail MCP tool."""

    @pytest.mark.asyncio
    async def test_trace_mail_success(self, mock_config):
        """Test successful mail trace returns graph data."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        trace_mail_tool = tools.get("mailtrace_trace_mail")
        assert trace_mail_tool is not None

        # Patch in the modules where they're imported from (inside the function)
        with (
            patch("mailtrace.trace.trace_mail_flow"),
            patch("mailtrace.mcp.tools.select_aggregator") as mock_select,
            patch("mailtrace.graph.MailGraph") as MockMailGraph,
        ):
            mock_select.return_value = MagicMock()

            # Mock MailGraph to return sample data
            mock_graph = MagicMock()
            mock_graph.to_dict.return_value = {
                "graph_dot": "digraph { a -> b }",
                "nodes": ["mail.example.com", "mail2.example.com"],
                "edges": [
                    {
                        "from": "mail.example.com",
                        "to": "mail2.example.com",
                        "mail_id": "ABC123DEF",
                    }
                ],
                "hop_count": 1,
            }
            MockMailGraph.return_value = mock_graph

            params = TraceMailInput(
                host="mail.example.com",
                mail_id="ABC123DEF",
            )

            result = await trace_mail_tool.fn(params)
            data = json.loads(result)

            assert "error" not in data
            assert "graph_dot" in data
            assert data["hop_count"] == 1
            assert len(data["edges"]) == 1
            assert data["trace"]["mail_ids"] == ["ABC123DEF"]
            assert data["trace"]["start_host"] == "mail.example.com"
            assert data["trace"]["search_mode"] == "mail_id"

    @pytest.mark.asyncio
    async def test_trace_mail_no_results(self, mock_config):
        """Test trace mail returns error when no hops found."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        trace_mail_tool = tools.get("mailtrace_trace_mail")

        # Patch in the modules where they're imported from (inside the function)
        with (
            patch("mailtrace.trace.trace_mail_flow"),
            patch("mailtrace.mcp.tools.select_aggregator") as mock_select,
            patch("mailtrace.graph.MailGraph") as MockMailGraph,
        ):
            mock_select.return_value = MagicMock()

            # Mock empty graph
            mock_graph = MagicMock()
            mock_graph.to_dict.return_value = {
                "graph_dot": "digraph { }",
                "nodes": [],
                "edges": [],
                "hop_count": 0,
            }
            MockMailGraph.return_value = mock_graph

            params = TraceMailInput(
                host="mail.example.com",
                mail_id="NONEXISTENT",
            )

            result = await trace_mail_tool.fn(params)
            data = json.loads(result)

            assert "error" in data
            assert data["error"]["code"] == "NO_RESULTS"

    @pytest.mark.asyncio
    async def test_trace_mail_invalid_time_format(self, mock_config):
        """Test trace mail returns error for invalid time format."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        trace_mail_tool = tools.get("mailtrace_trace_mail")

        with patch("mailtrace.mcp.tools.time_validation") as mock_time:
            mock_time.return_value = "Invalid time format"

            params = TraceMailInput(
                host="mail.example.com",
                mail_id="ABC123DEF",
                time="invalid-time",
                time_range="10h",
            )

            result = await trace_mail_tool.fn(params)
            data = json.loads(result)

            assert "error" in data
            assert data["error"]["code"] == "INVALID_TIME_FORMAT"

    @pytest.mark.asyncio
    async def test_trace_mail_config_error(self, mock_config):
        """Test trace mail handles FileNotFoundError as config error."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        trace_mail_tool = tools.get("mailtrace_trace_mail")

        with patch("mailtrace.mcp.tools.select_aggregator") as mock_select:
            mock_select.side_effect = FileNotFoundError("Config not found")

            params = TraceMailInput(
                host="mail.example.com",
                mail_id="ABC123DEF",
            )

            result = await trace_mail_tool.fn(params)
            data = json.loads(result)

            assert "error" in data
            assert data["error"]["code"] == "CONFIG_ERROR"

    @pytest.mark.asyncio
    async def test_trace_mail_connection_failed(self, mock_config):
        """Test trace mail handles generic exceptions as connection failure."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        trace_mail_tool = tools.get("mailtrace_trace_mail")

        with patch("mailtrace.mcp.tools.select_aggregator") as mock_select:
            mock_select.side_effect = RuntimeError("SSH connection failed")

            params = TraceMailInput(
                host="mail.example.com",
                mail_id="ABC123DEF",
            )

            result = await trace_mail_tool.fn(params)
            data = json.loads(result)

            assert "error" in data
            assert data["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.asyncio
    async def test_trace_mail_with_keywords(
        self, mock_config, sample_logs_by_id
    ):
        """Test trace mail using keywords instead of mail_id."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        trace_mail_tool = tools.get("mailtrace_trace_mail")

        with (
            patch("mailtrace.trace.trace_mail_flow"),
            patch("mailtrace.mcp.tools.select_aggregator") as mock_select,
            patch("mailtrace.mcp.tools.query_logs_by_keywords") as mock_query,
            patch("mailtrace.graph.MailGraph") as MockMailGraph,
        ):
            mock_select.return_value = MagicMock()
            mock_query.return_value = sample_logs_by_id

            mock_graph = MagicMock()
            mock_graph.to_dict.return_value = {
                "graph_dot": "digraph { a -> b }",
                "nodes": ["mail.example.com", "mail2.example.com"],
                "edges": [
                    {
                        "from": "mail.example.com",
                        "to": "mail2.example.com",
                        "mail_id": "ABC123DEF",
                    }
                ],
                "hop_count": 1,
            }
            MockMailGraph.return_value = mock_graph

            params = TraceMailInput(
                host="mail.example.com",
                keywords=["user@example.com"],
            )

            result = await trace_mail_tool.fn(params)
            data = json.loads(result)

            assert "error" not in data
            assert data["trace"]["search_mode"] == "keywords"
            assert data["trace"]["keywords"] == ["user@example.com"]
            assert "ABC123DEF" in data["trace"]["mail_ids"]

    @pytest.mark.asyncio
    async def test_trace_mail_keywords_no_results(self, mock_config):
        """Test trace mail with keywords returns error when no logs found."""
        mcp = FastMCP("test_mcp")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        trace_mail_tool = tools.get("mailtrace_trace_mail")

        with (
            patch("mailtrace.mcp.tools.select_aggregator") as mock_select,
            patch("mailtrace.mcp.tools.query_logs_by_keywords") as mock_query,
        ):
            mock_select.return_value = MagicMock()
            mock_query.return_value = {}

            params = TraceMailInput(
                host="mail.example.com",
                keywords=["nonexistent@example.com"],
            )

            result = await trace_mail_tool.fn(params)
            data = json.loads(result)

            assert "error" in data
            assert data["error"]["code"] == "NO_RESULTS"
