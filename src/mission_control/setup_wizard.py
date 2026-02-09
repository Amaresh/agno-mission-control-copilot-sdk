"""
Interactive setup wizard for Mission Control.

mc setup — detects dependencies, configures, initializes, and starts.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table

from mission_control import paths

console = Console()

# ─── Helpers ───────────────────────────────────────────────


def _run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture, text=True, check=check)


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _print_step(n: int, total: int, msg: str):
    console.print(f"\n[bold cyan][{n}/{total}][/bold cyan] {msg}")


def _ok(msg: str):
    console.print(f"  [green]✓[/green] {msg}")


def _warn(msg: str):
    console.print(f"  [yellow]⚠[/yellow]  {msg}")


def _fail(msg: str):
    console.print(f"  [red]✗[/red] {msg}")


# ─── Steps ─────────────────────────────────────────────────

TOTAL_STEPS = 11


def step_detect_system() -> dict:
    """Step 1: Detect system environment."""
    _print_step(1, TOTAL_STEPS, "Detecting system...")

    info = {
        "python": sys.version.split()[0],
        "os": "linux",
        "mc_home": str(paths.mc_home()),
    }

    py_major, py_minor = sys.version_info[:2]
    if py_major < 3 or py_minor < 11:
        _fail(f"Python {info['python']} detected — 3.11+ required")
        raise SystemExit(1)
    _ok(f"Python {info['python']}")

    import platform
    if platform.system() != "Linux":
        _warn(f"{platform.system()} detected — systemd services won't be installed")
        info["os"] = platform.system().lower()
    else:
        _ok("Linux detected")

    _ok(f"Data directory: {info['mc_home']}")
    return info


def step_github_cli() -> None:
    """Step 2: Detect or install GitHub CLI."""
    _print_step(2, TOTAL_STEPS, "GitHub CLI...")

    if _which("gh"):
        result = _run(["gh", "--version"])
        version = result.stdout.strip().split("\n")[0]
        _ok(f"Found: {version}")
    else:
        _fail("GitHub CLI (gh) not found")
        console.print("    Install: https://cli.github.com/")
        console.print("    Ubuntu/Debian: sudo apt install gh")
        raise SystemExit(1)


def step_copilot_extension() -> None:
    """Step 3: Detect or install Copilot CLI extension."""
    _print_step(3, TOTAL_STEPS, "GitHub Copilot CLI...")

    result = _run(["gh", "extension", "list"], check=False)
    if result.returncode == 0 and "copilot" in result.stdout.lower():
        _ok("Copilot CLI extension installed")
    else:
        console.print("  Installing gh copilot extension...")
        install = _run(["gh", "extension", "install", "github/gh-copilot"], check=False)
        if install.returncode == 0:
            _ok("Installed Copilot CLI extension")
        else:
            _fail("Could not install Copilot CLI extension")
            console.print("    Run: gh extension install github/gh-copilot")
            raise SystemExit(1)


def step_github_pat() -> Optional[str]:
    """Step 4: Configure GitHub PAT."""
    _print_step(4, TOTAL_STEPS, "GitHub Authentication...")

    # Check if already authed
    result = _run(["gh", "auth", "status"], check=False)
    if result.returncode == 0:
        _ok("Already authenticated with GitHub")
        # Extract token
        token_result = _run(["gh", "auth", "token"], check=False)
        if token_result.returncode == 0:
            return token_result.stdout.strip()

    console.print("  Agents need a GitHub PAT with [bold]repo[/bold] + [bold]copilot[/bold] scopes.")
    console.print("  Generate at: [link]https://github.com/settings/tokens[/link]")
    console.print()

    token = Prompt.ask("  GitHub PAT", password=True)
    if not token.startswith(("ghp_", "github_pat_")):
        _warn("Token doesn't look like a GitHub PAT — continuing anyway")

    # Authenticate
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    auth = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, env=env
    )
    if auth.returncode == 0:
        _ok("Token validated")
    else:
        _warn("Could not validate token — will use it anyway")

    return token


def step_database() -> str:
    """Step 5: Configure database."""
    _print_step(5, TOTAL_STEPS, "Database...")

    choice = Prompt.ask(
        "  Database backend",
        choices=["sqlite", "postgresql"],
        default="sqlite",
    )

    if choice == "sqlite":
        db_path = paths.db_path()
        url = f"sqlite+aiosqlite:///{db_path}"
        _ok(f"SQLite: {db_path}")
        return url

    # PostgreSQL
    host = Prompt.ask("  PostgreSQL host", default="localhost")
    port = Prompt.ask("  PostgreSQL port", default="5432")
    db = Prompt.ask("  Database name", default="mission_control")
    user = Prompt.ask("  Username", default="postgres")
    password = Prompt.ask("  Password", password=True, default="postgres")
    url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"
    _ok(f"PostgreSQL: {host}:{port}/{db}")
    return url


def step_telegram() -> tuple[Optional[str], Optional[str]]:
    """Step 6: Configure Telegram (optional)."""
    _print_step(6, TOTAL_STEPS, "Telegram (optional)...")

    if not Confirm.ask("  Configure Telegram bot?", default=False):
        _warn("Without Telegram, agents run with no human notification channel.")
        console.print("           Dashboard at :8000 is your only visibility into agent activity.")
        return None, None

    token = Prompt.ask("  Bot token (from @BotFather)")
    chat_id = Prompt.ask("  Chat ID")
    _ok("Telegram configured")
    return token, chat_id


def step_optional_mcp() -> dict:
    """Step 7: Optional MCP server tokens."""
    _print_step(7, TOTAL_STEPS, "Optional MCP servers...")
    console.print("  Skip any you don't need — agents degrade gracefully.")

    tokens = {}
    mcp_options = [
        ("DO_API_TOKEN", "DigitalOcean API token (for Quill infra monitor)"),
        ("TAVILY_API_KEY", "Tavily API key (web search for agents)"),
        ("TWILIO_ACCOUNT_SID", "Twilio Account SID (SMS/WhatsApp)"),
    ]

    for env_key, desc in mcp_options:
        val = Prompt.ask(f"  {desc}", default="")
        if val:
            tokens[env_key] = val
            if env_key == "TWILIO_ACCOUNT_SID":
                tokens["TWILIO_AUTH_TOKEN"] = Prompt.ask("  Twilio Auth Token")
                tokens["TWILIO_PHONE_NUMBER"] = Prompt.ask("  Twilio Phone Number")
            _ok(f"{env_key} set")

    if not tokens:
        _ok("No optional tokens — agents will use GitHub MCP only")

    return tokens


def step_write_env(
    github_token: str,
    database_url: str,
    telegram_token: Optional[str],
    telegram_chat_id: Optional[str],
    extra_tokens: dict,
    github_repo: Optional[str] = None,
) -> None:
    """Step 8: Write .env file."""
    _print_step(8, TOTAL_STEPS, "Writing configuration...")

    paths.ensure_dirs()
    env_path = paths.env_file()

    lines = [
        "# Mission Control — auto-generated by mc setup",
        f"GITHUB_TOKEN={github_token}",
    ]

    if github_repo:
        lines.append(f"GITHUB_REPO={github_repo}")

    lines.append(f"DATABASE_URL={database_url}")

    if telegram_token:
        lines.append(f"TELEGRAM_BOT_TOKEN={telegram_token}")
    if telegram_chat_id:
        lines.append(f"TELEGRAM_CHAT_ID={telegram_chat_id}")

    for k, v in extra_tokens.items():
        lines.append(f"{k}={v}")

    env_path.write_text("\n".join(lines) + "\n")
    _ok(f"Written: {env_path}")


def step_init_db(database_url: str) -> None:
    """Step 9: Initialize database and seed agents."""
    _print_step(9, TOTAL_STEPS, "Initializing database...")

    import asyncio

    # Temporarily set env so database.py picks it up
    os.environ["DATABASE_URL"] = database_url

    async def _init():
        # Re-import to pick up new DATABASE_URL
        from mission_control.mission_control.core.database import init_db, async_engine
        await init_db()
        _ok("Schema created")

    asyncio.run(_init())

    # Copy default configs if not present
    defaults = paths.defaults_dir()
    for fname, dest_name in [
        ("workflows.yaml.default", "workflows.yaml"),
        ("mcp_servers.yaml.default", "mcp_servers.yaml"),
    ]:
        src = defaults / fname
        dest = paths.config_dir() / dest_name
        if not dest.exists() and src.exists():
            shutil.copy2(src, dest)
            _ok(f"Copied default {dest_name}")
        elif dest.exists():
            _ok(f"{dest_name} already exists")

    # Seed agents
    _ok("Seeding agents...")
    _run([sys.executable, "-m", "mission_control.cli", "seed-agents"], check=False)
    _ok("Agents seeded")


def step_install_services(info: dict) -> None:
    """Step 10: Install systemd user services."""
    _print_step(10, TOTAL_STEPS, "Installing services...")

    if info.get("os") != "linux":
        _warn("Skipping systemd — not on Linux")
        return

    systemd_dir = paths.systemd_dir()
    systemd_dir.mkdir(parents=True, exist_ok=True)

    defaults = paths.defaults_dir() / "systemd"
    python_path = sys.executable
    mc_home_str = str(paths.mc_home())

    for template_file in defaults.glob("*.service"):
        content = template_file.read_text()
        content = content.replace("{PYTHON}", python_path)
        content = content.replace("{MC_HOME}", mc_home_str)

        dest = systemd_dir / template_file.name
        dest.write_text(content)
        _ok(f"Installed {template_file.name}")

    # Reload systemd
    _run(["systemctl", "--user", "daemon-reload"], check=False)
    _ok("Systemd reloaded")


def step_start_and_display(info: dict) -> None:
    """Step 11: Start services and display endpoints."""
    _print_step(11, TOTAL_STEPS, "Starting Mission Control...")

    if info.get("os") == "linux":
        services = ["mc-api", "mc-scheduler", "mc-bot", "mc-mcp"]
        for svc in services:
            _run(["systemctl", "--user", "enable", "--now", svc], check=False)

    console.print()
    console.print(Panel.fit(
        "[bold green]✅ Mission Control is running! (7 agents)[/bold green]\n\n"
        "📊 Dashboard:  http://localhost:8000/dashboard\n"
        "📋 Kanban:     http://localhost:8000/dashboard/kanban\n"
        "🔧 API Docs:   http://localhost:8000/docs\n"
        "🤖 Agents:    http://localhost:8000/agents\n"
        "📡 Workflow:   http://localhost:8000/workflow\n"
        "🔌 MCP:       http://localhost:8000/mcp/servers\n\n"
        "[bold]💾 Memory Profile (7 agents active):[/bold]\n"
        "   ~5.2 GB RAM required (each agent ≈ 565 MB)\n\n"
        "   Tight on RAM? Edit ~/.mission-control/workflows.yaml:\n"
        "   • 4 GB → Remove 2 agents (keep 5): ~3.7 GB\n"
        "   • 2 GB → Keep 3 agents (Jarvis, Friday, Vision): ~2.2 GB\n"
        "   • 1 GB → Single agent mode (Jarvis only): ~0.8 GB\n\n"
        "   Run [bold]mc status[/bold] to see current memory usage.",
        title="Mission Control",
        border_style="green",
    ))


# ─── Main ──────────────────────────────────────────────────


def run_setup() -> None:
    """Run the full interactive setup wizard."""
    console.print(Panel.fit(
        "[bold blue]Mission Control Setup[/bold blue]\n"
        "Interactive setup wizard for your autonomous agent squad.",
        border_style="blue",
    ))

    # Step 1: System detection
    info = step_detect_system()

    # Step 2-3: GitHub CLI + Copilot
    step_github_cli()
    step_copilot_extension()

    # Step 4: GitHub PAT
    github_token = step_github_pat()
    if not github_token:
        _fail("GitHub token required")
        raise SystemExit(1)

    # Ask for repo
    github_repo = Prompt.ask(
        "  Target GitHub repo (owner/repo)", default=""
    )

    # Step 5: Database
    database_url = step_database()

    # Step 6: Telegram
    tg_token, tg_chat_id = step_telegram()

    # Step 7: Optional MCP tokens
    extra_tokens = step_optional_mcp()

    # Step 8: Write .env
    step_write_env(
        github_token=github_token,
        database_url=database_url,
        telegram_token=tg_token,
        telegram_chat_id=tg_chat_id,
        extra_tokens=extra_tokens,
        github_repo=github_repo or None,
    )

    # Step 9: Init DB + seed
    step_init_db(database_url)

    # Step 10: Install systemd services
    step_install_services(info)

    # Step 11: Start + display
    step_start_and_display(info)
