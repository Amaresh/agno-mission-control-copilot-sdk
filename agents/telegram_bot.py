"""
Telegram Bot for Mission Control.

Listens for incoming messages and routes them to Jarvis.

Uses Agno patterns:
- Singleton agent instance (created once, reused)
- Session ID per chat for conversation continuity
- User ID for personalization
"""

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

import structlog
from telegram import Update
from telegram.error import TimedOut
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from agents.config import settings

logger = structlog.get_logger()

# Singleton Jarvis instance (Agno pattern: create once, reuse)
_jarvis_instance: Optional["JarvisAgent"] = None
_jarvis_lock = asyncio.Lock()


async def get_jarvis():
    """Get singleton Jarvis agent instance (async-safe)."""
    global _jarvis_instance
    if _jarvis_instance is None:
        async with _jarvis_lock:
            if _jarvis_instance is None:
                from agents.squad.jarvis.agent import JarvisAgent
                _jarvis_instance = JarvisAgent()
                # Pre-initialize the agent to avoid first-message delay
                await _jarvis_instance.get_agent()
                logger.info("Initialized singleton Jarvis agent")
    return _jarvis_instance


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        "🤖 *Jarvis here!*\n\n"
        "I'm the Squad Lead of Mission Control. Send me a message and I'll help you out.\n\n"
        "Available commands:\n"
        "/status - Check system status\n"
        "/agents - List available agents\n"
        "/standup - Get daily standup",
        parse_mode="Markdown",
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    try:
        from agents.mission_control.core.factory import AgentFactory
        agents = AgentFactory.list_agents()
        await update.message.reply_text(
            f"✅ *Mission Control Online*\n\n"
            f"Agents: {len(agents)}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Status command error", error=str(e), exc_info=True)
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def agents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /agents command."""
    from agents.mission_control.core.factory import AgentFactory
    agents = AgentFactory.list_agents()
    agent_list = "\n".join([f"• *{a['name']}* - {a['role']}" for a in agents])
    await update.message.reply_text(
        f"🤖 *Agent Squad*\n\n{agent_list}",
        parse_mode="Markdown",
    )


async def standup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /standup command."""
    logger.info("Standup command received", user=update.effective_user.first_name)
    await update.message.reply_text("⏳ Generating standup...")
    
    try:
        from agents.squad.jarvis.agent import create_jarvis
        jarvis = create_jarvis()
        summary = await jarvis.generate_daily_standup()
        logger.info("Standup generated", summary_len=len(summary))
        await update.message.reply_text(f"📋 *Daily Standup*\n\n{summary}", parse_mode="Markdown")
        logger.info("Standup sent to Telegram")
    except Exception as e:
        logger.error("Standup error", error=str(e), exc_info=True)
        await update.message.reply_text(f"❌ Error generating standup: {str(e)}")


async def _process_and_reply(bot, chat_id: int, user_message: str, user_id: int, message_id: int):
    """Process a message asynchronously and send the reply."""
    try:
        jarvis = await get_jarvis()

        # Keep typing indicator alive while processing
        async def keep_typing():
            try:
                while True:
                    await bot.send_chat_action(chat_id=chat_id, action="typing")
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                pass

        typing_task = asyncio.create_task(keep_typing())

        try:
            response = await jarvis.run(
                user_message,
                user_id=f"telegram_{user_id}",
                session_id=f"telegram_chat_{chat_id}",
            )
        finally:
            typing_task.cancel()

        logger.info("Sending Telegram reply", response_len=len(response) if response else 0)

        if not response or not response.strip():
            response = "⏳ I processed your request but got an empty response. Please try again."

        # Send reply with retry on TimedOut
        for attempt in range(2):
            try:
                if len(response) > 4000:
                    for i in range(0, len(response), 4000):
                        await bot.send_message(chat_id=chat_id, text=response[i:i+4000])
                else:
                    await bot.send_message(chat_id=chat_id, text=response)
                break
            except TimedOut:
                if attempt == 0:
                    logger.warning("Telegram reply timed out, retrying...")
                    await asyncio.sleep(2)
                else:
                    raise

        logger.info("Telegram reply sent successfully")

    except Exception as e:
        logger.error("Async message handling error", error=str(e), exc_info=True)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"❌ Sorry, I encountered an error: {str(e)}"
            )
        except Exception:
            logger.error("Failed to send error message to Telegram")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages by routing to Jarvis (non-blocking)."""
    user_message = update.message.text
    user_name = update.effective_user.first_name
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    logger.info(
        "Telegram message received",
        user=user_name,
        user_id=user_id,
        chat_id=chat_id,
        message_preview=user_message[:100],
    )

    # Fire off processing as a background task so the update loop stays free
    asyncio.create_task(
        _process_and_reply(context.bot, chat_id, user_message, user_id, update.message.message_id)
    )


def create_telegram_app() -> Application:
    """Create and configure the Telegram bot application."""
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN not configured")
    
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(15)
        .build()
    )
    
    # Add handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("agents", agents_command))
    app.add_handler(CommandHandler("standup", standup_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    return app


async def run_telegram_bot():
    """Run the Telegram bot with polling."""
    logger.info("Starting Telegram bot...")
    app = create_telegram_app()
    
    # Initialize and start polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    logger.info("Telegram bot is running!")
    
    # Keep running until interrupted
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


import sys


async def _start_mcp_servers() -> dict:
    """Start persistent MCP servers as background subprocesses.
    
    Returns dict of {name: asyncio.subprocess.Process}.
    Servers run in SSE mode so all Copilot sessions share one process each,
    instead of spawning a new subprocess per session.
    """
    import socket

    project_root = os.path.dirname(os.path.abspath(__file__))
    # Go up one level if we're inside agents/
    if os.path.basename(project_root) == "agents":
        project_root = os.path.dirname(project_root)
    
    venv_python = os.path.join(project_root, ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        venv_python = sys.executable

    def _port_open(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", port)) == 0

    processes = {}
    
    # 1. Mission Control MCP (Python FastMCP SSE on port 8001)
    mcp_port = int(os.environ.get("MCP_PORT", "8001"))

    # Skip if port is already in use (e.g., orphaned MCP server from a previous run)
    if _port_open(mcp_port):
        logger.warning("MCP port already in use, reusing existing server", port=mcp_port)
        return processes

    env = {
        **os.environ,
        "DATABASE_URL": settings.database_url or "",
        "PYTHONPATH": project_root,
        "MCP_PORT": str(mcp_port),
        "MCP_TRANSPORT": "sse",
    }
    # Redirect MCP stderr to log files instead of PIPE (prevents buffer overflow crash)
    mcp_log_dir = os.path.join(project_root, "logs")
    os.makedirs(mcp_log_dir, exist_ok=True)
    mc_stderr = open(os.path.join(mcp_log_dir, "mcp-mission-control.log"), "a")

    proc = await asyncio.create_subprocess_exec(
        venv_python, "-m", "agents.mission_control.mcp.mission_control_server",
        cwd=project_root,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=mc_stderr,
    )
    processes["mission-control"] = proc
    logger.info("Started Mission Control MCP server", port=mcp_port, pid=proc.pid)
    
    # Wait for SSE endpoint to be ready
    for attempt in range(15):
        await asyncio.sleep(1)
        if _port_open(mcp_port):
            logger.info("Mission Control MCP ready", port=mcp_port)
            break
    else:
        logger.warning("Mission Control MCP may not be ready yet — continuing anyway")
    
    # Note: GitHub MCP runs as type:"local" in Copilot config (per-session stdio subprocess).
    # Supergateway SSE bridge was removed — it only supports one concurrent client, which
    # breaks when multiple Copilot sessions connect simultaneously.
    
    return processes


async def run_telegram_bot_with_scheduler(with_scheduler: bool = False):
    """Run the Telegram bot.
    
    The scheduler is now a separate service (agents.scheduler_main / mc-scheduler.service).
    The with_scheduler flag is kept for backward compatibility but defaults to False.
    """
    logger.info("Starting Telegram bot...")
    
    # MCP server is now a separate systemd service (mc-mcp.service).
    # Only start it inline if it's not already running.
    mcp_processes = await _start_mcp_servers()
    
    # Pre-initialize Jarvis before starting bot (Agno pattern)
    await get_jarvis()
    
    app = create_telegram_app()
    
    # Legacy scheduler support (for non-systemd environments)
    scheduler = None
    if with_scheduler:
        from agents.mission_control.core.factory import AgentFactory
        from agents.mission_control.scheduler.heartbeat import get_scheduler
        
        scheduler = get_scheduler()
        for agent in AgentFactory.get_all_agents():
            scheduler.register_agent(agent.name, agent.heartbeat)
            logger.info(f"Registered agent for heartbeat", agent=agent.name)
        scheduler.start()
        logger.info("Heartbeat scheduler started (legacy inline mode)")
    
    # Initialize and start polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    logger.info("Telegram bot is running!")
    
    # Keep running until interrupted
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        if scheduler:
            scheduler.stop()
            logger.info("Heartbeat scheduler stopped")
        # Cleanup Copilot SDK sessions and client
        try:
            from agents.mission_control.core.copilot_model import _copilot_model
            if _copilot_model is not None:
                await _copilot_model.close()
                logger.info("Closed Copilot SDK client")
        except Exception as e:
            logger.warning("Failed to close Copilot client", error=str(e))
        # Terminate MCP server processes we started (not if reusing existing)
        for name, proc in mcp_processes.items():
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
                logger.info(f"Stopped MCP server: {name}")
            except Exception:
                proc.kill()
                logger.warning(f"Killed MCP server: {name}")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run_telegram_bot_with_scheduler())
