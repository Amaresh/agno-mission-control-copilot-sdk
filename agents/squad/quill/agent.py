"""
Quill — Infrastructure Ops / DigitalOcean Monitor.

Custom agent that runs DO health monitoring on every heartbeat,
regardless of whether there are assigned tasks.
"""

import asyncio
import time
from typing import Optional

import structlog

from agents.mission_control.learning.capture import capture_heartbeat

logger = structlog.get_logger()

# The monitoring prompt sent to the LLM on every heartbeat
MONITORING_PROMPT = """Run your infrastructure monitoring checklist NOW.

Use your DigitalOcean MCP tools to check:
1. List all apps — check each status (ACTIVE/DEPLOYING/ERROR)
2. List all databases — check each status (ONLINE/MAINTENANCE/etc)
3. List all droplets — check each status (active/off)
4. For any app NOT in ACTIVE state, get its recent deployments and logs
5. Check for any deployment failures in the last hour

After checking, write a summary following the format in your SOUL.md.
If any issues are found, report them clearly with severity.
If everything is healthy, confirm all-clear with counts.

IMPORTANT: Always produce a health summary, even if everything is green."""


def _create_quill_class():
    """Deferred import to avoid circular dependency with factory.py."""
    from agents.mission_control.core.factory import GenericAgent

    class QuillAgent(GenericAgent):
        """Quill — Infrastructure Ops agent specializing in DigitalOcean monitoring."""

        def __init__(self):
            super().__init__(
                name="Quill",
                role="Infrastructure Ops — DigitalOcean Monitor",
                session_key="agent:quill:main",
                mcp_servers=["digitalocean"],
                heartbeat_offset=16,
                level="specialist",
            )

        async def heartbeat(self) -> str:
            """
            Always run DO monitoring, then check for assigned tasks.

            Unlike other agents that idle when there's no work, Quill
            runs his monitoring checklist on every single heartbeat.
            """
            self.logger.info("Heartbeat started — DO monitoring cycle")
            t0 = time.monotonic()

            await self._record_heartbeat()

            # Phase 1: Always run infrastructure monitoring
            try:
                monitoring_result = await asyncio.wait_for(
                    self.run(MONITORING_PROMPT, user_id="system"),
                    timeout=self.HEARTBEAT_WORK_TIMEOUT,
                )
                self.logger.info(
                    "DO monitoring complete",
                    result_len=len(monitoring_result) if monitoring_result else 0,
                )
            except asyncio.TimeoutError:
                monitoring_result = "TIMEOUT: DO monitoring exceeded time limit"
                self.logger.warning("DO monitoring timed out")
            except Exception as e:
                monitoring_result = f"ERROR: DO monitoring failed — {e}"
                self.logger.error("DO monitoring failed", error=str(e))

            # Phase 2: Check for any assigned tasks (normal heartbeat flow)
            work = await self._check_for_work()
            if work:
                self.logger.info("Also found assigned work", work_type=work.get("type"))
                try:
                    await asyncio.wait_for(
                        self._do_work(work),
                        timeout=self.HEARTBEAT_WORK_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    self.logger.warning("Task work timed out")

            duration = time.monotonic() - t0
            await capture_heartbeat(
                agent_name=self.name,
                found_work=True,  # always did monitoring
                work_type="do_monitoring",
                duration_seconds=duration,
            )

            # Write health summary to daily log
            summary_line = monitoring_result[:300] if monitoring_result else "No output"
            self.append_daily_note(f"## DO Health Check\n{summary_line}")

            return monitoring_result or "HEARTBEAT_OK"

    return QuillAgent


# Module-level factory function
def create_quill_agent():
    """Create a QuillAgent instance (deferred import pattern)."""
    QuillAgent = _create_quill_class()
    return QuillAgent()
