"""Tests for flow-check MCP tool."""

import json
from unittest.mock import MagicMock, patch

import pytest

from mailtrace.config import Config, Method
from mailtrace.flow_check import (
    FlowCheckResult,
    FlowStatus,
    MailFlow,
)
from mailtrace.mcp.tools import CheckFlowInput, register_tools
from mcp.server.fastmcp import FastMCP


@pytest.fixture
def mock_config():
    config = MagicMock(spec=Config)
    config.method = Method.SSH
    return config


class TestCheckFlowInput:
    def test_minimal_input(self):
        inp = CheckFlowInput(cluster="mx-cluster")
        assert inp.cluster == "mx-cluster"
        assert inp.time is None
        assert inp.time_range == "1h"

    def test_cluster_required(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CheckFlowInput()


class TestCheckFlowTool:
    @pytest.mark.asyncio
    async def test_success(self, mock_config):
        mcp = FastMCP("test")
        register_tools(mcp, mock_config)

        tools = mcp._tool_manager._tools
        tool = tools.get("mailtrace_check_flow")
        assert tool is not None

        mock_result = FlowCheckResult(
            cluster="mx-cluster",
            time="2025-01-15 10:00:00",
            time_range="1h",
            keywords=None,
            out_of_window_mail_ids=[],
            summary={
                "total_inbound": 1,
                "complete": 1,
                "problematic": 0,
            },
            complete_flows=[
                MailFlow(
                    inbound_mail_id="ABC123",
                    inbound_host="mx1.example.com",
                    source="external.com",
                    status=FlowStatus.COMPLETE,
                    terminal_state="delivered_locally",
                )
            ],
            problematic_flows=[],
        )

        with (
            patch("mailtrace.mcp.tools.check_cluster_flow") as mock_check,
            patch("mailtrace.mcp.tools.select_aggregator") as mock_select,
        ):
            mock_select.return_value = MagicMock()
            mock_check.return_value = mock_result

            params = CheckFlowInput(cluster="mx-cluster")
            result = await tool.fn(params)
            data = json.loads(result)

            assert "error" not in data
            assert data["summary"]["complete"] == 1

    @pytest.mark.asyncio
    async def test_cluster_not_found(self, mock_config):
        mcp = FastMCP("test")
        register_tools(mcp, mock_config)

        tool = mcp._tool_manager._tools["mailtrace_check_flow"]

        with (
            patch("mailtrace.mcp.tools.check_cluster_flow") as mock_check,
            patch("mailtrace.mcp.tools.select_aggregator") as mock_select,
        ):
            mock_select.return_value = MagicMock()
            mock_check.side_effect = ValueError("Cluster 'bad' not found")

            params = CheckFlowInput(cluster="bad")
            result = await tool.fn(params)
            data = json.loads(result)

            assert data["error"]["code"] == "INVALID_CLUSTER"
