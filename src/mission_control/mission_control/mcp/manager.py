"""
MCP (Model Context Protocol) Manager for Mission Control.

Manages connections to external MCP servers.
Supports both SSE (persistent, shared) and stdio (per-agent) modes.

SSE mode (default): Connects to pre-running MCP servers via URL.
Stdio mode (fallback): Spawns subprocess per connection.
"""

from dataclasses import dataclass
from typing import Optional

import structlog
from agno.tools.mcp import MCPTools

from mission_control.mission_control.mcp.repo_scoped import RepoScopedMCPTools

logger = structlog.get_logger()


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server."""
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    description: str
    # SSE URL when running as persistent server (preferred)
    sse_url: Optional[str] = None


class MCPManager:
    """Manages MCP server connections and tools."""

    def __init__(self):
        self._configs = self._build_configs()
        self._tools_cache: dict[str, MCPTools] = {}

    def _build_configs(self) -> dict[str, MCPServerConfig]:
        """Build MCP server configurations from registry (mcp_servers.yaml)."""
        from mission_control.mission_control.mcp.registry import get_mcp_registry
        registry = get_mcp_registry()
        configs = {}
        for server_info in registry.list_servers():
            name = server_info["name"]
            if server_info.get("builtin"):
                continue  # mission-control is wired separately
            config = registry.get_server_config(name)
            if config:
                configs[name] = config
        return configs

    def get_available_servers(self) -> list[str]:
        """Get list of available MCP server names."""
        return list(self._configs.keys())

    def get_server_config(self, name: str) -> Optional[MCPServerConfig]:
        """Get configuration for a specific server."""
        return self._configs.get(name)

    async def get_tools_for_agent(self, server_names: list[str]) -> list[MCPTools]:
        """Get MCP tools for an agent based on their assigned servers.
        
        Prefers SSE connection to persistent server when available,
        falls back to stdio subprocess spawn.
        """
        tools = []

        for name in server_names:
            config = self._configs.get(name)
            if not config:
                logger.warning(f"MCP server '{name}' not configured, skipping")
                continue

            try:
                # Use RepoScopedMCPTools for GitHub to prevent wrong-repo writes
                ToolClass = RepoScopedMCPTools if name == "github" else MCPTools

                if config.sse_url:
                    mcp_tool = ToolClass(
                        url=config.sse_url,
                        transport="sse",
                        timeout_seconds=30,
                        tool_name_prefix=f"{name}_",
                    )
                else:
                    from mcp.client.stdio import StdioServerParameters
                    server_params = StdioServerParameters(
                        command=config.command,
                        args=config.args,
                        env=config.env,
                    )
                    mcp_tool = ToolClass(
                        server_params=server_params,
                        timeout_seconds=30,
                        tool_name_prefix=f"{name}_",
                    )
                tools.append(mcp_tool)
                logger.info(f"Loaded MCP tools for '{name}'",
                           mode="sse" if config.sse_url else "stdio")
            except Exception as e:
                logger.error(f"Failed to load MCP '{name}': {e}")

        return tools
