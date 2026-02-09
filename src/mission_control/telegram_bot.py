"""
Telegram Bot for Mission Control.

Listens for incoming messages and routes them to Jarvis (normal mode)
or Vision (escalation mode).

Chat mode state machine:
  [JARVIS] ─── /vision or auto-escalate ──► [VISION]
     ▲                                          │
     └──── /jarvis or confirmed handback ───────┘

Vision mode is activated when Jarvis fails or the user types /vision.
Vision executes commands via Copilot CLI (Opus 4.6) with --allow-all.
"""

import asyncio
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import structlog
from telegram import Update
from telegram.error import TimedOut
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from mission_control.config import settings

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Singleton Jarvis
# ---------------------------------------------------------------------------
_jarvis_instance = None
_jarvis_lock = asyncio.Lock()


async def get_jarvis():
    """Get singleton Jarvis agent instance (async-safe)."""
    global _jarvis_instance
    if _jarvis_instance is None:
        async with _jarvis_lock:
            if _jarvis_instance is None:
                from mission_control.mission_control.core.factory import AgentFactory
                _jarvis_instance = AgentFactory.get_agent("jarvis")
                await _jarvis_instance.get_agent()
                logger.info("Initialized singleton Jarvis agent")
    return _jarvis_instance


# ---------------------------------------------------------------------------
# Singleton Vision (for Telegram escalation)
# ---------------------------------------------------------------------------
_vision_instance: Optional["VisionHealer"] = None
_vision_lock = asyncio.Lock()


async def get_vision():
    """Get singleton Vision healer instance (async-safe)."""
    global _vision_instance
    if _vision_instance is None:
        async with _vision_lock:
            if _vision_instance is None:
                from mission_control.mission_control.core.factory import AgentFactory
                _vision_instance = AgentFactory.get_agent("vision")
                logger.info("Initialized singleton Vision healer for Telegram")
    return _vision_instance


# ---------------------------------------------------------------------------
# Per-chat mode state (in-memory — resets on service restart)
# ---------------------------------------------------------------------------
# "jarvis" or "vision"
_chat_mode: dict[int, str] = defaultdict(lambda: "jarvis")
# Per-chat lock to prevent concurrent mode transitions
_chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
# Count of consecutive Vision responses (for handback suggestion)
_vision_streak: dict[int, int] = defaultdict(int)
# Timestamp of last auto-escalation (for cooldown / silent re-escalation)
_last_escalation: dict[int, float] = defaultdict(float)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        "🤖 *Jarvis here!*\n\n"
        "I'm the Squad Lead of Mission Control. Send me a message and I'll help you out.\n\n"
        "Available commands:\n"
        "/status - Check system status\n"
        "/agents - List available agents\n"
        "/standup - Get daily standup\n"
        "/vision - Switch to Vision (ops/crisis mode)\n"
        "/jarvis - Switch back to Jarvis (normal mode)",
        parse_mode="Markdown",
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    try:
        from mission_control.mission_control.core.factory import AgentFactory
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
    from mission_control.mission_control.core.factory import AgentFactory
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
        from mission_control.mission_control.core.factory import AgentFactory
        jarvis = AgentFactory.get_agent("jarvis")
        summary = await jarvis.generate_daily_standup()
        logger.info("Standup generated", summary_len=len(summary))
        await update.message.reply_text(f"📋 *Daily Standup*\n\n{summary}", parse_mode="Markdown")
        logger.info("Standup sent to Telegram")
    except Exception as e:
        logger.error("Standup error", error=str(e), exc_info=True)
        await update.message.reply_text(f"❌ Error generating standup: {str(e)}")


async def vision_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /vision — switch chat to Vision (ops escalation) mode."""
    chat_id = update.effective_chat.id
    async with _chat_locks[chat_id]:
        prev = _chat_mode[chat_id]
        _chat_mode[chat_id] = "vision"
        _vision_streak[chat_id] = 0

    if prev == "vision":
        await update.message.reply_text("👁️ Already in Vision mode. Send commands directly.")
    else:
        await update.message.reply_text(
            "👁️ *Switched to Vision mode*\n\n"
            "Send shell commands, gh CLI commands, or natural-language ops instructions.\n"
            "Vision (Opus 4.6) will execute them directly.\n\n"
            "Type /jarvis to switch back.",
            parse_mode="Markdown",
        )
    logger.info("Manual switch to Vision mode", chat_id=chat_id)


async def jarvis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /jarvis — switch chat back to Jarvis (normal) mode."""
    chat_id = update.effective_chat.id
    async with _chat_locks[chat_id]:
        prev = _chat_mode[chat_id]
        _chat_mode[chat_id] = "jarvis"
        _vision_streak[chat_id] = 0

    if prev == "jarvis":
        await update.message.reply_text("🤖 Already in Jarvis mode.")
    else:
        await update.message.reply_text(
            "🤖 *Switched back to Jarvis mode*\n\n"
            "Normal LLM routing restored.",
            parse_mode="Markdown",
        )
    logger.info("Manual switch to Jarvis mode", chat_id=chat_id)


def _build_progress_message(tools_seen: list[str], tasks_created: int, elapsed: float) -> str:
    """Build a human-readable progress message from observed tool activity."""
    elapsed_min = int(elapsed // 60)
    elapsed_sec = int(elapsed % 60)
    time_str = f"{elapsed_min}m {elapsed_sec}s" if elapsed_min else f"{elapsed_sec}s"

    tool_counts: dict[str, int] = {}
    for t in tools_seen:
        tool_counts[t] = tool_counts.get(t, 0) + 1

    activities: list[str] = []
    if "list_pull_requests" in tool_counts or "get_pull_request" in tool_counts:
        activities.append("🔍 Reviewing pull requests")
    if "search_code" in tool_counts or "get_file_contents" in tool_counts:
        activities.append("🔎 Reading code")
    if tasks_created:
        activities.append(f"📝 Created {tasks_created} task(s) so far")
    if "list_tasks" in tool_counts or "get_my_tasks" in tool_counts:
        activities.append("📋 Checking existing tasks")
    if "list_issues" in tool_counts or "search_issues" in tool_counts:
        activities.append("🐛 Checking issues")
    if "create_branch" in tool_counts:
        activities.append("🌿 Creating branch")
    if "create_or_update_file" in tool_counts:
        activities.append("✏️ Writing files")
    if "create_pull_request" in tool_counts:
        activities.append("🔀 Creating pull request")
    if not activities:
        activities.append(f"⚙️ Processing ({len(tools_seen)} operations)")

    return f"⏳ Still working... ({time_str})\n" + "\n".join(activities)


async def _send_progress_updates(bot, chat_id: int, queue: asyncio.Queue):
    """Read tool events from queue and send periodic Telegram progress updates."""
    start = time.monotonic()
    tools_seen: list[str] = []
    tasks_created = 0
    last_update = 0.0
    initial_delay = 10  # seconds before first update
    interval = 20       # seconds between subsequent updates

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=2)
                if event["type"] == "tool_start":
                    tools_seen.append(event["tool"])
                    if event["tool"] == "create_task":
                        tasks_created += 1
            except asyncio.TimeoutError:
                pass

            elapsed = time.monotonic() - start
            since_update = time.monotonic() - last_update

            should_send = (
                tools_seen
                and elapsed >= initial_delay
                and (last_update == 0.0 or since_update >= interval)
            )
            if should_send:
                msg = _build_progress_message(tools_seen, tasks_created, elapsed)
                try:
                    await bot.send_message(chat_id=chat_id, text=msg)
                except Exception:
                    pass  # Best-effort progress update; Telegram errors are non-fatal
                last_update = time.monotonic()
                tools_seen.clear()
    except asyncio.CancelledError:
        pass


async def _log_activity(activity_type, message: str, extra_data: dict = None):
    """Log a chatbot activity to the DB."""
    from mission_control.mission_control.core.database import AsyncSessionLocal, Activity, ActivityType
    try:
        async with AsyncSessionLocal() as session:
            act = Activity(
                type=getattr(ActivityType, activity_type),
                message=message,
                extra_data=extra_data or {},
            )
            session.add(act)
            await session.commit()
            return str(act.id)
    except Exception as e:
        logger.warning("Failed to log activity", type=activity_type, error=str(e))
        return None


async def _send_reply(bot, chat_id: int, response: str):
    """Send a response to Telegram with chunking and retry."""
    for attempt in range(2):
        try:
            if len(response) > 4000:
                for i in range(0, len(response), 4000):
                    await bot.send_message(chat_id=chat_id, text=response[i:i + 4000])
            else:
                await bot.send_message(chat_id=chat_id, text=response)
            return
        except TimedOut:
            if attempt == 0:
                logger.warning("Telegram reply timed out, retrying…")
                await asyncio.sleep(2)
            else:
                raise


async def _run_jarvis(user_message: str, user_id: int, chat_id: int, bot) -> str:
    """Run message through Jarvis with typing + progress indicators.

    Raises on failure so the caller can escalate.
    """
    jarvis = await get_jarvis()

    async def keep_typing():
        try:
            while True:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(keep_typing())

    from mission_control.mission_control.core.copilot_model import progress_queue_var
    pq: asyncio.Queue = asyncio.Queue()
    token = progress_queue_var.set(pq)
    progress_task = asyncio.create_task(
        _send_progress_updates(bot, chat_id, pq)
    )

    try:
        response = await jarvis.run(
            user_message,
            user_id=f"telegram_{user_id}",
            session_id=f"telegram_chat_{chat_id}",
        )
    finally:
        progress_task.cancel()
        typing_task.cancel()
        for t in (progress_task, typing_task):
            try:
                await t
            except asyncio.CancelledError:
                pass
        progress_queue_var.reset(token)

    if not response or not response.strip():
        raise RuntimeError("Jarvis returned empty response")
    return response


async def _run_vision(user_message: str, bot, chat_id: int) -> str:
    """Run message through Vision's execute_command (Copilot CLI, Opus 4.6)."""

    async def keep_typing():
        try:
            while True:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(keep_typing())
    try:
        vision = await get_vision()
        response = await vision.execute_command(user_message)
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

    return response or "⚠️ Vision returned no output."


# Auto-escalation cooldown — suppress repeated "Switching to Vision" messages
_ESCALATION_COOLDOWN = 300  # 5 min


async def _process_and_reply(bot, chat_id: int, user_message: str, user_id: int, message_id: int):
    """Route message to Jarvis or Vision based on chat mode, with auto-escalation."""
    start_mono = time.monotonic()

    received_id = await _log_activity(
        "MESSAGE_RECEIVED",
        f"Telegram message from user {user_id}",
        {"chat_id": chat_id, "user_id": user_id,
         "message_text": user_message, "message_len": len(user_message)},
    )

    try:
        async with _chat_locks[chat_id]:
            mode = _chat_mode[chat_id]

        response: str | None = None

        if mode == "vision":
            # ------- Vision mode: execute directly -------
            response = await _run_vision(user_message, bot, chat_id)

            async with _chat_locks[chat_id]:
                _vision_streak[chat_id] += 1
                streak = _vision_streak[chat_id]

            # After 2+ consecutive successful Vision responses, check health
            if streak >= 2:
                vision = await get_vision()
                if await vision.quick_health_check():
                    response += (
                        "\n\n---\n"
                        "✅ Systems look stable. Hand back to Jarvis? "
                        "Type /jarvis to switch, or keep sending commands."
                    )

        else:
            # ------- Jarvis mode: try Jarvis, auto-escalate on failure -------
            try:
                response = await _run_jarvis(user_message, user_id, chat_id, bot)
            except Exception as jarvis_err:
                logger.warning(
                    "Jarvis failed, auto-escalating to Vision",
                    error=str(jarvis_err),
                    chat_id=chat_id,
                )

                # Flip to Vision mode (atomically)
                async with _chat_locks[chat_id]:
                    _chat_mode[chat_id] = "vision"
                    _vision_streak[chat_id] = 1
                    last_esc = _last_escalation[chat_id]
                    _last_escalation[chat_id] = time.monotonic()

                # Notify user (suppress if re-escalated within cooldown)
                if (time.monotonic() - last_esc) > _ESCALATION_COOLDOWN:
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=(
                                "⚠️ Jarvis failed — switching to *Vision* (ops mode).\n"
                                f"Error: `{str(jarvis_err)[:200]}`\n\n"
                                "Retrying your message with Vision…"
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass  # Best-effort escalation notice; continue to Vision retry

                # Retry with Vision
                response = await _run_vision(user_message, bot, chat_id)

        # ------- Deliver response -------
        elapsed_ms = int((time.monotonic() - start_mono) * 1000)
        logger.info(
            "Sending Telegram reply",
            mode=mode,
            response_len=len(response) if response else 0,
            elapsed_ms=elapsed_ms,
        )
        await _log_activity(
            "MESSAGE_RESPONDED",
            f"Replied in {elapsed_ms}ms (mode={_chat_mode[chat_id]})",
            {"request_activity_id": received_id, "response_time_ms": elapsed_ms,
             "response_len": len(response) if response else 0, "chat_id": chat_id},
        )

        await _send_reply(bot, chat_id, response)
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
    app.add_handler(CommandHandler("vision", vision_command))
    app.add_handler(CommandHandler("jarvis", jarvis_command))
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
    
    The scheduler is now a separate service (mission_control.scheduler_main / mc-scheduler.service).
    The with_scheduler flag is kept for backward compatibility but defaults to False.
    """
    logger.info("Starting Telegram bot...")

    # Sync agent configs to DB (ensures roles/levels match code)
    from mission_control.mission_control.core.factory import AgentFactory
    await AgentFactory.sync_agent_configs()
    
    # MCP server is now a separate systemd service (mc-mcp.service).
    # Only start it inline if it's not already running.
    mcp_processes = await _start_mcp_servers()
    
    # Pre-initialize Jarvis before starting bot (Agno pattern)
    await get_jarvis()
    
    app = create_telegram_app()
    
    # Legacy scheduler support (for non-systemd environments)
    scheduler = None
    if with_scheduler:
        from mission_control.mission_control.core.factory import AgentFactory
        from mission_control.mission_control.scheduler.heartbeat import get_scheduler
        
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
            from mission_control.mission_control.core.copilot_model import _copilot_model
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
