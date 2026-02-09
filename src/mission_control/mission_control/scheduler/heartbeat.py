"""
Heartbeat scheduler for Mission Control agents.

Implements staggered 15-minute heartbeats for all agents.
"""

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = structlog.get_logger()


# Agent heartbeat schedule (minute offset within each 15-min window)
# Vision is excluded — it runs hourly via register_hourly_agent()
AGENT_SCHEDULE = {
    "jarvis": 0,   # :00, :15, :30, :45
    "friday": 2,   # :02, :17, :32, :47
    "wong": 6,     # :06, :21, :36, :51
    "shuri": 8,    # :08, :23, :38, :53
    "fury": 10,    # :10, :25, :40, :55
    "pepper": 12,  # :12, :27, :42, :57
    "loki": 14,    # :14, :29, :44, :59
    "quill": 1,    # :01, :16, :31, :46
    "wanda": 3,    # :03, :18, :33, :48
}


class HeartbeatScheduler:
    """
    Scheduler for agent heartbeats.

    Each agent wakes up every 15 minutes at their designated offset
    to check for work.
    """

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._agent_callbacks: dict[str, Callable[[], Awaitable[str]]] = {}
        self.logger = logger.bind(component="scheduler")

    def register_agent(
        self,
        agent_name: str,
        heartbeat_callback: Callable[[], Awaitable[str]],
    ):
        """
        Register an agent for heartbeat scheduling.

        Args:
            agent_name: Name of the agent (e.g., "jarvis")
            heartbeat_callback: Async function to call on heartbeat
        """
        agent_key = agent_name.lower()

        if agent_key not in AGENT_SCHEDULE:
            self.logger.warning(f"Unknown agent '{agent_name}', using default offset")
            offset = (len(self._agent_callbacks) * 2) % 15  # clamp to 0-14
        else:
            offset = AGENT_SCHEDULE[agent_key]

        self._agent_callbacks[agent_key] = heartbeat_callback

        # Schedule every 15 minutes at the agent's offset
        # e.g., offset=2 means :02, :17, :32, :47
        minutes = f"{offset},{offset+15},{offset+30},{offset+45}"

        self.scheduler.add_job(
            self._run_heartbeat,
            CronTrigger(minute=minutes),
            args=[agent_key],
            id=f"heartbeat_{agent_key}",
            name=f"Heartbeat: {agent_name}",
            replace_existing=True,
        )

        self.logger.info(
            "Registered agent for heartbeat",
            agent=agent_name,
            schedule=f":{minutes.replace(',', ', :')}",
        )

    def register_hourly_agent(
        self,
        agent_name: str,
        heartbeat_callback: Callable[[], Awaitable[str]],
        minute_offset: int = 5,
    ):
        """Register an agent that runs once per hour instead of every 15 minutes."""
        agent_key = agent_name.lower()
        self._agent_callbacks[agent_key] = heartbeat_callback

        self.scheduler.add_job(
            self._run_heartbeat,
            CronTrigger(minute=minute_offset),
            args=[agent_key],
            id=f"heartbeat_{agent_key}",
            name=f"Heartbeat (hourly): {agent_name}",
            replace_existing=True,
        )

        self.logger.info(
            "Registered agent for hourly heartbeat",
            agent=agent_name,
            schedule=f":{minute_offset} every hour",
        )

    HEARTBEAT_TIMEOUT = 300  # 5 minutes max per heartbeat

    async def _run_heartbeat(self, agent_key: str):
        """Execute heartbeat for an agent with enforced timeout."""
        callback = self._agent_callbacks.get(agent_key)
        if not callback:
            self.logger.error(f"No callback registered for agent '{agent_key}'")
            return

        self.logger.info("Running heartbeat", agent=agent_key)
        start_time = datetime.now(timezone.utc)

        try:
            result = await asyncio.wait_for(
                callback(),
                timeout=self.HEARTBEAT_TIMEOUT,
            )
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()

            self.logger.info(
                "Heartbeat completed",
                agent=agent_key,
                result=result[:100] if result else "OK",
                duration_seconds=duration,
            )
        except asyncio.TimeoutError:
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            self.logger.warning(
                "Heartbeat timed out",
                agent=agent_key,
                timeout=self.HEARTBEAT_TIMEOUT,
                duration_seconds=duration,
            )
        except Exception as e:
            self.logger.error(
                "Heartbeat failed",
                agent=agent_key,
                error=str(e),
            )

    def start(self):
        """Start the scheduler."""
        self.scheduler.start()
        self.logger.info("Heartbeat scheduler started")

    def stop(self):
        """Stop the scheduler."""
        self.scheduler.shutdown()
        self.logger.info("Heartbeat scheduler stopped")

    def get_next_runs(self) -> dict[str, datetime]:
        """Get next scheduled run time for each agent."""
        next_runs = {}
        for job in self.scheduler.get_jobs():
            if job.id.startswith("heartbeat_"):
                agent = job.id.replace("heartbeat_", "")
                next_runs[agent] = job.next_run_time
        return next_runs


# Global scheduler instance
_scheduler: HeartbeatScheduler | None = None


def get_scheduler() -> HeartbeatScheduler:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = HeartbeatScheduler()
    return _scheduler
