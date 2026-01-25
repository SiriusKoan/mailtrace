"""MCP server setup and configuration."""

from mailtrace.config import Config
from mailtrace.mcp.tools import register_resources, register_tools
from mcp.server.fastmcp import FastMCP


def create_server(config: Config, port: int = 8080) -> FastMCP:
    """Create and configure the MCP server with tools."""
    mcp = FastMCP("mailtrace_mcp", port=port)
    register_tools(mcp, config)
    register_resources(mcp, config)
    return mcp


def run_server(
    config: Config,
    transport: str = "stdio",
    port: int = 8080,
) -> None:
    """Run the MCP server with specified transport."""
    mcp = create_server(config, port=port)

    if transport == "stdio":
        mcp.run()
    elif transport == "sse":
        mcp.run(transport="sse")
    else:
        raise ValueError(f"Unknown transport: {transport}")
