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
def status():
    """Show status of all agents and the system."""
    from agents.mission_control.core.factory import AgentFactory

    console.print(Panel.fit(
        "[bold blue]Mission Control[/bold blue] - Agent Status",
        border_style="blue",
    ))

    # Agent table
    table = Table(title="Agents")
    table.add_column("Name", style="cyan")
    table.add_column("Role", style="green")
    table.add_column("MCP Servers", style="yellow")
    table.add_column("Status", style="magenta")

    for agent_info in AgentFactory.list_agents():
        table.add_row(
            agent_info["name"],
            agent_info["role"],
            ", ".join(agent_info["mcp_servers"]),
            "Ready",
        )

    console.print(table)


@app.command()
def init_db():
    """Initialize the database."""
    from agents.mission_control.core.database import init_db

    console.print("[yellow]Initializing database...[/yellow]")
    asyncio.run(init_db())
    console.print("[green]✓ Database initialized successfully[/green]")


@app.command()
def seed_agents():
    """Seed the database with agent records."""
    from agents.mission_control.core.database import Agent as AgentModel
    from agents.mission_control.core.database import AsyncSessionLocal
    from agents.mission_control.core.factory import AGENT_CONFIGS

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
    from agents.mission_control.core.factory import AgentFactory

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
    from agents.mission_control.core.factory import AgentFactory

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
    """Start the Mission Control daemon with scheduler."""
    from agents.mission_control.core.factory import AgentFactory
    from agents.mission_control.scheduler.heartbeat import get_scheduler

    console.print(Panel.fit(
        "[bold blue]Mission Control[/bold blue] - Starting...",
        border_style="blue",
    ))

    # Initialize scheduler
    scheduler = get_scheduler()

    # Register all agents
    for agent in AgentFactory.get_all_agents():
        scheduler.register_agent(agent.name, agent.heartbeat)
        console.print(f"[green]✓ Registered: {agent.name}[/green]")

    # Start scheduler
    scheduler.start()
    console.print("[bold green]Mission Control is running![/bold green]")
    console.print("[dim]Press Ctrl+C to stop[/dim]")

    # Keep running
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        scheduler.stop()
        console.print("\n[yellow]Mission Control stopped[/yellow]")


@app.command()
def task(
    title: str = typer.Option(..., "--title", "-t", help="Task title"),
    description: str = typer.Option("", "--desc", "-d", help="Task description"),
    assign: Optional[str] = typer.Option(None, "--assign", "-a", help="Assign to agent"),
):
    """Create a new task."""
    from agents.squad.jarvis.agent import create_jarvis

    async def _create():
        jarvis = create_jarvis()
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
    from agents.squad.jarvis.agent import create_jarvis

    async def _standup():
        jarvis = create_jarvis()
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

    from agents.api import app as api_app

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
    from agents.config import settings

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

    from agents.telegram_bot import run_telegram_bot_with_scheduler
    asyncio.run(run_telegram_bot_with_scheduler(with_scheduler=with_scheduler))


if __name__ == "__main__":
    app()
