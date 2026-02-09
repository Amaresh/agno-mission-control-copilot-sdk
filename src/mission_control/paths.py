"""Centralized path resolution for Mission Control.

All file paths (config, DB, logs, agent notes) resolve through here.
Supports two modes:
  - Development: uses project root (detected via .git or pyproject.toml)
  - Installed:   uses ~/.mission-control/
"""

import os
import sys
from pathlib import Path
from functools import lru_cache

# Override everything with MC_HOME env var
_MC_HOME_ENV = os.environ.get("MC_HOME")


@lru_cache(maxsize=1)
def mc_home() -> Path:
    """Root directory for all Mission Control user data."""
    if _MC_HOME_ENV:
        return Path(_MC_HOME_ENV).expanduser().resolve()

    # Development mode: if running from a git repo with pyproject.toml
    dev_root = _find_project_root()
    if dev_root:
        return dev_root

    # Installed mode: ~/.mission-control/
    return Path.home() / ".mission-control"


def _find_project_root() -> Path | None:
    """Walk up from CWD looking for pyproject.toml with our project name."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        toml = parent / "pyproject.toml"
        if toml.exists():
            try:
                text = toml.read_text()
                if 'name = "mission-control"' in text:
                    return parent
            except OSError:
                pass
        if parent == parent.parent:
            break
    return None


def config_dir() -> Path:
    """Directory containing workflows.yaml, mcp_servers.yaml, .env."""
    return mc_home()


def db_path() -> Path:
    """Default SQLite database path."""
    return mc_home() / "mission_control.db"


def logs_dir() -> Path:
    """Directory for log files."""
    d = mc_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def squad_dir() -> Path:
    """Root directory for agent working files (daily notes, etc.)."""
    d = mc_home() / "squad"
    d.mkdir(parents=True, exist_ok=True)
    return d


def agent_dir(agent_name: str) -> Path:
    """Working directory for a specific agent."""
    d = squad_dir() / agent_name / "daily"
    d.mkdir(parents=True, exist_ok=True)
    return d


def workflows_yaml() -> Path:
    """Path to workflows.yaml config file."""
    return config_dir() / "workflows.yaml"


def mcp_servers_yaml() -> Path:
    """Path to mcp_servers.yaml config file."""
    return config_dir() / "mcp_servers.yaml"


def env_file() -> Path:
    """Path to .env file."""
    return config_dir() / ".env"


def systemd_dir() -> Path:
    """User systemd service directory."""
    return Path.home() / ".config" / "systemd" / "user"


def defaults_dir() -> Path:
    """Directory containing shipped default config files."""
    return Path(__file__).parent / "defaults"


def ensure_dirs() -> None:
    """Create all required directories. Called by mc setup."""
    mc_home().mkdir(parents=True, exist_ok=True)
    logs_dir()
    squad_dir()
