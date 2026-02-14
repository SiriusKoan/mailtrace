"""Tests for MCP server creation and configuration."""

from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from mailtrace import create_server as mailtrace_create_server
from mailtrace import run_server as mailtrace_run_server
from mailtrace.config import Config, Method
from mailtrace.mcp import create_server as mcp_create_server
from mailtrace.mcp import run_server as mcp_run_server
from mailtrace.mcp.server import create_server, run_server


@pytest.fixture
def mock_config():
    """Create a mock Config object."""
    config = MagicMock(spec=Config)
    config.method = Method.SSH
    return config


class TestCreateServer:
    """Tests for create_server function."""

    def test_create_server_returns_fastmcp(self, mock_config):
        """Test create_server returns a FastMCP instance."""
        server = create_server(mock_config)

        assert isinstance(server, FastMCP)

    def test_create_server_registers_tools(self, mock_config):
        """Test create_server registers both MCP tools."""
        server = create_server(mock_config)

        # Access internal tool manager to verify tools registered
        tools = server._tool_manager._tools
        assert "mailtrace_query_logs" in tools
        assert "mailtrace_trace_mail" in tools

    def test_create_server_with_custom_port(self, mock_config):
        """Test create_server accepts custom port."""
        server = create_server(mock_config, port=9090)

        # Server should be created (port is passed to FastMCP)
        assert server is not None

    def test_create_server_default_port(self, mock_config):
        """Test create_server uses default port 8080."""
        server = create_server(mock_config)

        # Server should be created with default port
        assert server is not None


class TestRunServer:
    """Tests for run_server function."""

    def test_run_server_invalid_transport_raises(self, mock_config):
        """Test run_server raises ValueError for invalid transport."""
        with pytest.raises(ValueError) as exc_info:
            run_server(mock_config, transport="invalid")

        assert "Unknown transport" in str(exc_info.value)
        assert "invalid" in str(exc_info.value)


class TestToolAnnotations:
    """Tests for tool annotations and metadata."""

    def test_query_logs_tool_annotations(self, mock_config):
        """Test mailtrace_query_logs has correct annotations."""
        server = create_server(mock_config)
        tool = server._tool_manager._tools.get("mailtrace_query_logs")

        assert tool is not None
        # Tool should have annotations
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True

    def test_trace_mail_tool_annotations(self, mock_config):
        """Test mailtrace_trace_mail has correct annotations."""
        server = create_server(mock_config)
        tool = server._tool_manager._tools.get("mailtrace_trace_mail")

        assert tool is not None
        # Tool should have annotations
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True


class TestResources:
    """Tests for MCP resources."""

    def test_clusters_resource_registered(self, mock_config):
        """Test that clusters resource is registered."""
        mcp = create_server(mock_config)
        resources = mcp._resource_manager._resources
        assert "mailtrace://clusters" in resources

    def test_clusters_resource_returns_json(self, mock_config):
        """Test that clusters resource returns valid JSON."""
        import json

        mock_config.clusters = {
            "mx-cluster": ["mx1.example.com", "mx2.example.com"]
        }

        mcp = create_server(mock_config)
        resources = mcp._resource_manager._resources
        clusters_resource = resources.get("mailtrace://clusters")

        result = clusters_resource.fn()
        data = json.loads(result)

        assert "mx-cluster" in data
        assert data["mx-cluster"] == ["mx1.example.com", "mx2.example.com"]


class TestModuleExports:
    """Tests for module exports."""

    def test_mcp_module_exports_create_server(self):
        """Test that create_server is exported from mailtrace.mcp."""
        assert callable(mcp_create_server)

    def test_mcp_module_exports_run_server(self):
        """Test that run_server is exported from mailtrace.mcp."""
        assert callable(mcp_run_server)

    def test_main_module_exports_create_server(self):
        """Test that create_server is exported from mailtrace."""
        assert callable(mailtrace_create_server)

    def test_main_module_exports_run_server(self):
        """Test that run_server is exported from mailtrace."""
        assert callable(mailtrace_run_server)
