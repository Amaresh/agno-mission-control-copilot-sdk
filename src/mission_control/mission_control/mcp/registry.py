"""
MCP Server Registry — config-driven MCP server management.

Loads server definitions from mcp_servers.yaml, resolves env vars,
and provides MCPServerConfig objects to the MCPManager.
Replaces the hardcoded _build_configs() approach.
"""

import os
from pathlib import Path
from typing import Optional

import structlog
import yaml

from mission_control.mission_control.mcp.manager import MCPServerConfig

logger = structlog.get_logger()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_PATH = _PROJECT_ROOT / "mcp_servers.yaml"

# Singleton
_registry: Optional["MCPRegistry"] = None


class MCPRegistry:
    """Loads mcp_servers.yaml and resolves env vars for each server."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or _DEFAULT_PATH
        self._servers: dict[str, dict] = {}
        self._availability: dict[str, dict] = {}
        self.load()

    def load(self):
        """Load or reload mcp_servers.yaml."""
        if not self._path.exists():
            logger.warning("mcp_servers.yaml not found, using empty registry", path=str(self._path))
            self._servers = {}
            self._availability = {}
            return

        try:
            with open(self._path) as f:
                data = yaml.safe_load(f) or {}
            self._servers = data.get("servers", {})
            self._resolve_availability()
            logger.info(
                "MCP registry loaded",
                servers=len(self._servers),
                available=sum(1 for v in self._availability.values() if v["available"]),
            )
        except Exception as e:
            logger.error("Failed to load mcp_servers.yaml", error=str(e))
            self._servers = {}
            self._availability = {}

    def _resolve_availability(self):
        """Check which servers have all required env vars set."""
        self._availability = {}
        for name, cfg in self._servers.items():
            env_keys = cfg.get("env_keys", [])
            missing = [k for k in env_keys if not os.environ.get(k)]
            self._availability[name] = {
                "available": len(missing) == 0,
                "missing_env": missing,
            }

    def get_server_config(self, name: str) -> Optional[MCPServerConfig]:
        """Build an MCPServerConfig for the named server, or None if unavailable."""
        cfg = self._servers.get(name)
        if not cfg:
            return None

        avail = self._availability.get(name, {})
        if not avail.get("available", False):
            logger.warning(
                "MCP server unavailable — missing env vars",
                server=name,
                missing=avail.get("missing_env", []),
            )
            return None

        # Build env dict from env_map
        env = {}
        env_map = cfg.get("env_map", {})
        for target_var, source_var in env_map.items():
            val = os.environ.get(source_var, "")
            if val:
                env[target_var] = val

        # Build args — resolve args_template if present
        args = list(cfg.get("args", []))
        args_template = cfg.get("args_template")
        if args_template:
            # Resolve {VAR} placeholders
            resolved = args_template
            for key in cfg.get("env_keys", []):
                resolved = resolved.replace(f"{{{key}}}", os.environ.get(key, ""))
            args.extend(resolved.split())

        return MCPServerConfig(
            name=name,
            command=cfg.get("command", "npx"),
            args=args,
            env=env,
            description=cfg.get("description", ""),
        )

    def get_tools_allowlist(self, name: str) -> Optional[list[str]]:
        """Return the tools allowlist for a server, or None if not specified."""
        cfg = self._servers.get(name, {})
        return cfg.get("tools")

    def get_configs_for_agent(self, server_names: list[str]) -> dict[str, MCPServerConfig]:
        """Get all available MCPServerConfigs for an agent's server list."""
        result = {}
        for name in server_names:
            config = self.get_server_config(name)
            if config:
                result[name] = config
            elif name not in self._servers:
                logger.warning("Agent references unknown MCP server", server=name)
        return result

    def list_servers(self) -> list[dict]:
        """Return all servers with availability status (for GET /mcp/servers)."""
        result = []
        for name, cfg in self._servers.items():
            avail = self._availability.get(name, {})
            entry = {
                "name": name,
                "available": avail.get("available", False),
                "description": cfg.get("description", ""),
            }
            if not avail.get("available", False):
                entry["missing_env"] = avail.get("missing_env", [])
            result.append(entry)
        # Always include mission-control as builtin
        result.append({
            "name": "mission-control",
            "available": True,
            "description": "Mission Control task/agent status (built-in)",
            "builtin": True,
        })
        return result

    def reload(self):
        """Alias for load() — used during hot-reload."""
        self.load()


def get_mcp_registry() -> MCPRegistry:
    """Get or create the singleton MCPRegistry."""
    global _registry
    if _registry is None:
        _registry = MCPRegistry()
    return _registry


def reload_mcp_registry():
    """Force reload of the MCP registry (called on POST /workflow)."""
    global _registry
    if _registry is not None:
        _registry.reload()
    else:
        _registry = MCPRegistry()
