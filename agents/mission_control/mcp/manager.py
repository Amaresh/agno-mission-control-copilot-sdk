"""
MCP (Model Context Protocol) Manager for Mission Control.

Manages connections to external MCP servers.
Supports both SSE (persistent, shared) and stdio (per-agent) modes.

SSE mode (default): Connects to pre-running MCP servers via URL.
Stdio mode (fallback): Spawns subprocess per connection.
"""

import os
from typing import Optional
from dataclasses import dataclass

import structlog
from agno.tools.mcp import MCPTools

from agents.config import settings
from agents.mission_control.mcp.repo_scoped import RepoScopedMCPTools

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
        """Build MCP server configurations from settings."""
        configs = {}

        # GitHub MCP - Official ModelContextProtocol server
        # Note: Runs as stdio subprocess (not SSE) — supergateway SSE bridge
        # doesn't support concurrent clients, so Copilot CLI manages GitHub MCP
        # as type:"local" per-session subprocess.
        if settings.github_token:
            configs["github"] = MCPServerConfig(
                name="github",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-github"],
                env={"GITHUB_PERSONAL_ACCESS_TOKEN": settings.github_token},
                description="GitHub repository, issues, and PR management",
            )

        # Tavily MCP - Official Tavily server for web search
        if settings.tavily_api_key:
            configs["tavily"] = MCPServerConfig(
                name="tavily",
                command="npx",
                args=["-y", "tavily-mcp"],
                env={"TAVILY_API_KEY": settings.tavily_api_key},
                description="Web search and research",
            )

        # Twilio MCP - Community server for SMS
        if settings.twilio_account_sid and settings.twilio_auth_token:
            configs["twilio"] = MCPServerConfig(
                name="twilio",
                command="npx",
                args=["-y", "twilio-mcp"],
                env={
                    "TWILIO_ACCOUNT_SID": settings.twilio_account_sid,
                    "TWILIO_AUTH_TOKEN": settings.twilio_auth_token,
                    "TWILIO_PHONE_NUMBER": settings.twilio_phone_number or "",
                },
                description="Twilio SMS messaging",
            )

        # Telegram MCP - Community server for Telegram
        if settings.telegram_bot_token:
            configs["telegram"] = MCPServerConfig(
                name="telegram",
                command="npx",
                args=["-y", "@zhigang1992/telegram-mcp"],
                env={
                    "TELEGRAM_BOT_TOKEN": settings.telegram_bot_token,
                },
                description="Telegram messaging",
            )

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

    def get_agent_mcp_mapping(self) -> dict[str, list[str]]:
        """Get the default agent-to-MCP mapping."""
        return {
            "jarvis": ["github", "digitalocean", "telegram"],
            "friday": ["github"],
            "vision": ["digitalocean"],
            "wong": ["github"],
            "shuri": ["github"],
            "fury": ["tavily"],
            "pepper": ["twilio"],
        }

    def get_mcps_for_agent(self, agent_name: str) -> list[str]:
        """Get MCP server names for a specific agent."""
        mapping = self.get_agent_mcp_mapping()
        return mapping.get(agent_name.lower(), [])
