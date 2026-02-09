"""
Standalone heartbeat scheduler + watchdog for Mission Control.

Runs independently from the Telegram bot so heartbeats continue
even if the bot crashes.

Usage: python -m mission_control.scheduler_main
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta

import structlog
from sqlalchemy import select, func

from mission_control.config import settings
from mission_control.mission_control.core.factory import AgentFactory, _get_agent_configs
from mission_control.mission_control.scheduler.heartbeat import get_scheduler

logger = structlog.get_logger()

# Watchdog: alert if an agent's heartbeat exceeds its expected interval
# plus a grace period.  Agents on a 15-min schedule get 30 min;
# hourly agents (like Vision) get 65 min.
WATCHDOG_GRACE_MINUTES = 5

# Suppress repeated alerts: track when we last alerted per agent.
# Only re-alert after WATCHDOG_SUPPRESS_MINUTES for the same agent.
WATCHDOG_SUPPRESS_MINUTES = 120  # 2 hours
_last_watchdog_alert: dict[str, datetime] = {}


async def _check_heartbeat_health():
    """Watchdog: alert human via Telegram if heartbeats go stale."""
    from mission_control.mission_control.core.database import AsyncSessionLocal, Agent as AgentModel

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(AgentModel))
            all_agents = result.scalars().all()

            now = datetime.now(timezone.utc)
            stale_agents = []
            configs = _get_agent_configs()
            for a in all_agents:
                key = a.name.lower()
                interval_sec = configs.get(key, {}).get("heartbeat_interval", 900)
                interval_min = interval_sec / 60  # convert to minutes
                threshold = interval_min + WATCHDOG_GRACE_MINUTES
                if a.last_heartbeat is None or (now - a.last_heartbeat).total_seconds() > threshold * 60:
                    stale_agents.append((a.name, threshold))

            if not stale_agents:
                return

            # Suppress repeated alerts — only re-alert after WATCHDOG_SUPPRESS_MINUTES
            now_check = datetime.now(timezone.utc)
            unsuppressed = []
            for name, threshold in stale_agents:
                last_alert = _last_watchdog_alert.get(name)
                if last_alert is None or (now_check - last_alert).total_seconds() > WATCHDOG_SUPPRESS_MINUTES * 60:
                    unsuppressed.append((name, threshold))
                    _last_watchdog_alert[name] = now_check

            if not unsuppressed:
                logger.debug("Stale agents suppressed (already alerted recently)",
                             agents=[n for n, _ in stale_agents])
                return

            names = [name for name, _ in unsuppressed]
            logger.warning("Stale heartbeats detected", agents=names)

            # Send Telegram alert
            chat_id = settings.telegram_chat_id
            bot_token = settings.telegram_bot_token
            if chat_id and bot_token:
                import httpx
                message = (
                    f"⚠️ *Heartbeat Watchdog Alert*\n\n"
                    f"The following agents have stale heartbeats:\n"
                    + "\n".join(f"• {n} (>{t}min)" for n, t in unsuppressed)
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

    # Sync agent configs to DB before anything else
    await AgentFactory.sync_agent_configs()

    scheduler = get_scheduler()

    # Register all agents for heartbeat — read interval from config
    configs = _get_agent_configs()
    for agent in AgentFactory.get_all_agents():
        key = agent.name.lower()
        interval_sec = configs.get(key, {}).get("heartbeat_interval", 900)  # default 15 min
        if interval_sec >= 3600:
            # Hourly (or longer) agents
            scheduler.register_hourly_agent(agent.name, agent.heartbeat, minute_offset=5)
            logger.info("Registered agent (hourly)", agent=agent.name, interval=interval_sec)
        else:
            scheduler.register_agent(agent.name, agent.heartbeat)
            logger.info("Registered agent", agent=agent.name, interval=interval_sec)

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

    # Add learning aggregation job — runs every 30 minutes
    from mission_control.mission_control.learning.processor import process_learning_events
    scheduler.scheduler.add_job(
        process_learning_events,
        IntervalTrigger(minutes=30),
        id="learning_aggregation",
        name="Learning Event Aggregation",
        replace_existing=True,
    )
    logger.info("Registered learning aggregation", interval="30min")

    # Add daily standup job at 18:00 UTC (11:30 PM IST)
    chat_id = settings.telegram_chat_id
    bot_token = settings.telegram_bot_token

    if chat_id and bot_token:
        async def _scheduled_standup():
            try:
                from mission_control.mission_control.core.factory import AgentFactory
                import httpx
                jarvis = AgentFactory.get_agent("jarvis")
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
