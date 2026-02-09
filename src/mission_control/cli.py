"""
CLI for Mission Control.
"""

import asyncio
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(
    name="mc",
    help="Mission Control - Autonomous Multi-Agent System",
    add_completion=False,
)
console = Console()


@app.command()
def setup():
    """Interactive setup wizard — configures and starts Mission Control."""
    from mission_control.setup_wizard import run_setup
    run_setup()


@app.command()
def status():
    """Show agent status, service health, and memory usage."""
    import subprocess

    from mission_control.mission_control.core.factory import AgentFactory

    console.print(Panel.fit(
        "[bold blue]Mission Control[/bold blue] — Status",
        border_style="blue",
    ))

    # Agent table
    table = Table(title="Agents")
    table.add_column("Name", style="cyan")
    table.add_column("Role", style="green")
    table.add_column("MCP Servers", style="yellow")

    for agent_info in AgentFactory.list_agents():
        table.add_row(
            agent_info["name"],
            agent_info["role"],
            ", ".join(agent_info["mcp_servers"]),
        )
    console.print(table)

    # Service health
    console.print()
    services = ["mc-api", "mc-scheduler", "mc-bot", "mc-mcp"]
    svc_table = Table(title="Services")
    svc_table.add_column("Service", style="cyan")
    svc_table.add_column("Status", style="green")

    for svc in services:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", svc],
            capture_output=True, text=True,
        )
        state = result.stdout.strip()
        style = "green" if state == "active" else "red" if state == "failed" else "yellow"
        svc_table.add_row(svc, f"[{style}]{state}[/{style}]")

    console.print(svc_table)

    # Memory
    try:
        import resource
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        console.print(f"\n[dim]CLI process memory: {mem_mb:.0f} MB[/dim]")
    except Exception:
        pass

    agent_count = len(AgentFactory.list_agents())
    est_gb = agent_count * 0.565
    console.print(f"[dim]Estimated total ({agent_count} agents): ~{est_gb:.1f} GB RAM[/dim]")


@app.command()
def init_db():
    """Initialize the database."""
    from mission_control.mission_control.core.database import init_db

    console.print("[yellow]Initializing database...[/yellow]")
    asyncio.run(init_db())
    console.print("[green]✓ Database initialized successfully[/green]")


@app.command()
def seed_agents():
    """Seed the database with agent records."""
    from mission_control.mission_control.core.database import Agent as AgentModel
    from mission_control.mission_control.core.database import AsyncSessionLocal
    from mission_control.mission_control.core.factory import AGENT_CONFIGS

    async def _seed():
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            for key, config in AGENT_CONFIGS.items():
                # Check if exists
                stmt = select(AgentModel).where(AgentModel.name == config["name"])
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if not existing:
                    agent = AgentModel(
                        name=config["name"],
                        role=config["role"],
                        session_key=config["session_key"],
                        mcp_servers=config["mcp_servers"],
                        heartbeat_offset_minutes=config["heartbeat_offset"],
                    )
                    session.add(agent)
                    console.print(f"[green]✓ Created agent: {config['name']}[/green]")
                else:
                    console.print(f"[dim]Agent already exists: {config['name']}[/dim]")

            await session.commit()

    asyncio.run(_seed())
    console.print("[green]✓ Agents seeded successfully[/green]")


@app.command()
def run(
    agent: str = typer.Argument(..., help="Agent name to run"),
    message: str = typer.Argument(..., help="Message to send to the agent"),
):
    """Run an agent with a message."""
    from mission_control.mission_control.core.factory import AgentFactory

    async def _run():
        agent_instance = AgentFactory.get_agent(agent)
        console.print(f"[cyan]Running {agent}...[/cyan]")

        response = await agent_instance.run(message)
        console.print(Panel(response, title=f"{agent}'s Response", border_style="green"))

    asyncio.run(_run())


@app.command()
def heartbeat(
    agent: Optional[str] = typer.Argument(None, help="Agent name (or all)"),
):
    """Trigger a heartbeat for an agent or all agents."""
    from mission_control.mission_control.core.factory import AgentFactory

    async def _heartbeat():
        if agent:
            agent_instance = AgentFactory.get_agent(agent)
            console.print(f"[cyan]Running heartbeat for {agent}...[/cyan]")
            result = await agent_instance.heartbeat()
            console.print(f"[green]Result: {result}[/green]")
        else:
            for agent_instance in AgentFactory.get_all_agents():
                console.print(f"[cyan]Running heartbeat for {agent_instance.name}...[/cyan]")
                try:
                    result = await agent_instance.heartbeat()
                    console.print(f"[green]  → {result}[/green]")
                except Exception as e:
                    console.print(f"[red]  → Error: {e}[/red]")

    asyncio.run(_heartbeat())


@app.command()
def start():
    """Start all Mission Control services via systemd."""
    import subprocess

    services = ["mc-api", "mc-scheduler", "mc-bot", "mc-mcp"]
    console.print(Panel.fit(
        "[bold blue]Mission Control[/bold blue] — Starting services...",
        border_style="blue",
    ))

    for svc in services:
        result = subprocess.run(
            ["systemctl", "--user", "start", svc],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            console.print(f"  [green]✓[/green] {svc}")
        else:
            console.print(f"  [red]✗[/red] {svc}: {result.stderr.strip()}")

    console.print("\n[bold green]Services started.[/bold green] Run [bold]mc status[/bold] to check health.")


@app.command()
def stop():
    """Stop all Mission Control services."""
    import subprocess

    services = ["mc-api", "mc-scheduler", "mc-bot", "mc-mcp"]
    for svc in services:
        subprocess.run(
            ["systemctl", "--user", "stop", svc],
            capture_output=True, text=True,
        )
        console.print(f"  [yellow]■[/yellow] Stopped {svc}")

    console.print("[yellow]All services stopped.[/yellow]")


@app.command()
def logs(
    agent: Optional[str] = typer.Argument(None, help="Agent name or service (mc-api, mc-scheduler)"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    lines: int = typer.Option(50, "--lines", "-n", help="Number of lines to show"),
):
    """View Mission Control logs."""
    import subprocess

    if agent and agent.startswith("mc-"):
        service = agent
    elif agent:
        service = "mc-scheduler"  # agent heartbeats go through scheduler
    else:
        service = "mc-api"

    cmd = ["journalctl", "--user", "-u", service, f"--lines={lines}", "--no-pager"]
    if follow:
        cmd.append("-f")

    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass


@app.command()
def config():
    """Show current configuration paths and key settings."""
    from mission_control import paths
    from mission_control.config import settings

    table = Table(title="Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Home directory", str(paths.mc_home()))
    table.add_row("Workflows", str(paths.workflows_yaml()))
    table.add_row("MCP servers", str(paths.mcp_servers_yaml()))
    table.add_row("Environment", str(paths.env_file()))
    table.add_row("Logs", str(paths.logs_dir()))
    table.add_row("Database", settings.database_url.split("@")[-1] if "@" in settings.database_url else settings.database_url[:80])
    table.add_row("Telegram", "✓ configured" if settings.telegram_bot_token else "✗ not set")
    table.add_row("GitHub", "✓ configured" if settings.github_token else "✗ not set")

    console.print(table)


@app.command()
def task(
    title: str = typer.Option(..., "--title", "-t", help="Task title"),
    description: str = typer.Option("", "--desc", "-d", help="Task description"),
    assign: Optional[str] = typer.Option(None, "--assign", "-a", help="Assign to agent"),
):
    """Create a new task."""
    from mission_control.mission_control.core.factory import AgentFactory

    async def _create():
        jarvis = AgentFactory.get_agent("jarvis")
        assignees = [assign] if assign else []

        task_id = await jarvis.create_task(
            title=title,
            description=description,
            assignees=assignees,
        )

        console.print(f"[green]✓ Created task: {task_id}[/green]")
        if assign:
            console.print(f"[cyan]  Assigned to: {assign}[/cyan]")

    asyncio.run(_create())


@app.command()
def standup():
    """Generate daily standup summary."""
    from mission_control.mission_control.core.factory import AgentFactory

    async def _standup():
        jarvis = AgentFactory.get_agent("jarvis")
        summary = await jarvis.generate_daily_standup()
        console.print(Panel(summary, title="Daily Standup", border_style="blue"))

    asyncio.run(_standup())


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Host to bind"),
    port: int = typer.Option(8000, "--port", "-p", help="Port to bind"),
):
    """Start the HTTP API server for chat interactions."""
    import uvicorn

    from mission_control.api import app as api_app

    console.print(Panel.fit(
        f"[bold blue]Mission Control API[/bold blue]\n"
        f"Running at http://{host}:{port}\n\n"
        f"Endpoints:\n"
        f"  POST /chat          - Chat with Jarvis\n"
        f"  POST /chat/{{agent}} - Chat with specific agent\n"
        f"  POST /task          - Create a task\n"
        f"  GET  /agents        - List agents\n"
        f"  GET  /standup       - Daily standup",
        border_style="blue",
    ))

    uvicorn.run(api_app, host=host, port=port)


@app.command()
def telegram(
    with_scheduler: bool = typer.Option(True, "--scheduler/--no-scheduler", help="Run heartbeat scheduler"),
):
    """Start the Telegram bot to receive and respond to messages."""
    from mission_control.config import settings

    if not settings.telegram_bot_token:
        console.print("[red]Error: TELEGRAM_BOT_TOKEN not configured in .env[/red]")
        raise typer.Exit(1)

    scheduler_status = "✅ Enabled" if with_scheduler else "❌ Disabled"
    console.print(Panel.fit(
        "[bold blue]Mission Control Telegram Bot[/bold blue]\n"
        f"Heartbeat Scheduler: {scheduler_status}\n"
        "Listening for messages...\n\n"
        "Commands:\n"
        "  /start   - Welcome message\n"
        "  /status  - System status\n"
        "  /agents  - List agents\n"
        "  /standup - Daily standup",
        border_style="blue",
    ))

    from mission_control.telegram_bot import run_telegram_bot_with_scheduler
    asyncio.run(run_telegram_bot_with_scheduler(with_scheduler=with_scheduler))


if __name__ == "__main__":
    app()
