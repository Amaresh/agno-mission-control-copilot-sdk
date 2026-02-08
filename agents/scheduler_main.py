"""
Standalone heartbeat scheduler + watchdog for Mission Control.

Runs independently from the Telegram bot so heartbeats continue
even if the bot crashes.

Usage: python -m agents.scheduler_main
"""

import asyncio
from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from agents.config import settings
from agents.mission_control.core.factory import AgentFactory
from agents.mission_control.scheduler.heartbeat import get_scheduler

logger = structlog.get_logger()

# Watchdog: alert if an agent's heartbeat exceeds its expected interval
# plus a grace period.  Agents on a 15-min schedule get 20 min;
# hourly agents (like Vision) get 65 min.
WATCHDOG_GRACE_MINUTES = 5


async def _check_heartbeat_health():
    """Watchdog: alert human via Telegram if heartbeats go stale."""
    from agents.mission_control.core.database import Agent as AgentModel
    from agents.mission_control.core.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(AgentModel))
            all_agents = result.scalars().all()

            now = datetime.now(timezone.utc)
            stale_agents = []
            for a in all_agents:
                # Vision runs hourly; all others run every 15 min
                interval = 60 if a.name.lower() == "vision" else 15
                threshold = interval + WATCHDOG_GRACE_MINUTES
                if a.last_heartbeat is None or (now - a.last_heartbeat).total_seconds() > threshold * 60:
                    stale_agents.append((a.name, threshold))

            if not stale_agents:
                return

            names = [name for name, _ in stale_agents]
            logger.warning("Stale heartbeats detected", agents=names)

            # Send Telegram alert
            chat_id = settings.telegram_chat_id
            bot_token = settings.telegram_bot_token
            if chat_id and bot_token:
                import httpx
                message = (
                    "⚠️ *Heartbeat Watchdog Alert*\n\n"
                    "The following agents have stale heartbeats:\n"
                    + "\n".join(f"• {n} (>{t}min)" for n, t in stale_agents)
                    + f"\n\nTime: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                )
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
                        timeout=10,
                    )
                logger.info("Sent watchdog alert to Telegram", stale_agents=names)
            else:
                logger.warning("No Telegram credentials for watchdog alert")

    except Exception as e:
        logger.error("Watchdog check failed", error=str(e))


async def _run():
    """Async entry point — APScheduler needs a running event loop."""
    from apscheduler.triggers.interval import IntervalTrigger

    logger.info("Starting standalone heartbeat scheduler...")

    scheduler = get_scheduler()

    # Register all agents for heartbeat
    for agent in AgentFactory.get_all_agents():
        if agent.name.lower() == "vision":
            # Vision Healer runs hourly, not every 15 minutes
            scheduler.register_hourly_agent(agent.name, agent.heartbeat, minute_offset=5)
            logger.info("Registered agent (hourly)", agent=agent.name)
        else:
            scheduler.register_agent(agent.name, agent.heartbeat)
            logger.info("Registered agent", agent=agent.name)

    # Add watchdog job — runs every 10 minutes
    # AsyncIOScheduler natively supports coroutine functions
    scheduler.scheduler.add_job(
        _check_heartbeat_health,
        IntervalTrigger(minutes=10),
        id="heartbeat_watchdog",
        name="Heartbeat Watchdog",
        replace_existing=True,
    )
    logger.info("Registered heartbeat watchdog", interval="10min", grace_minutes=WATCHDOG_GRACE_MINUTES)

    # Add daily standup job at 18:00 UTC (11:30 PM IST)
    chat_id = settings.telegram_chat_id
    bot_token = settings.telegram_bot_token

    if chat_id and bot_token:
        async def _scheduled_standup():
            try:
                import httpx

                from agents.squad.jarvis.agent import create_jarvis
                jarvis = create_jarvis()
                summary = await jarvis.generate_daily_standup()
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": f"📋 *Daily Standup*\n\n{summary}",
                            "parse_mode": "Markdown",
                        },
                        timeout=30,
                    )
                logger.info("Sent scheduled standup")
            except Exception as e:
                logger.error("Scheduled standup failed", error=str(e))

        from apscheduler.triggers.cron import CronTrigger
        scheduler.scheduler.add_job(
            _scheduled_standup,
            CronTrigger(hour=18, minute=0),
            id="daily_standup",
            name="Daily Standup",
            replace_existing=True,
        )
        logger.info("Registered daily standup", schedule="18:00 UTC")

    scheduler.start()
    logger.info("Scheduler running.")

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        scheduler.stop()
        logger.info("Scheduler stopped.")


def main():
    """Entry point."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
