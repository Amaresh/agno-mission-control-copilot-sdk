"""
Vision Healer Agent — deterministic system health monitor.

Does NOT use Copilot SDK or LLM for health checks. Runs a checklist
of deterministic checks every hour. For code fixes, shells out to
`copilot` CLI with GPT-4.1 (free tier GitHub model).

Reports issues via Telegram + GitHub Issue.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

import structlog
from agents.squad.vision.checks import HealthCheckResult, run_all_checks
from agents.squad.vision.notify import notify_human
from sqlalchemy import select

logger = structlog.get_logger()


class VisionHealer:
    """
    System healer that runs deterministic health checks.

    Not a BaseAgent subclass — it doesn't need LLM, MCP tools,
    or Agno sessions. It's a pure ops agent.
    """

    def __init__(self):
        self.name = "Vision"
        self.logger = logger.bind(agent="Vision", role="Healer")

    async def _record_heartbeat(self):
        """Persist last_heartbeat timestamp so the watchdog doesn't flag Vision as stale."""
        from agents.mission_control.core.database import (
            Activity,
            ActivityType,
            AsyncSessionLocal,
        )
        from agents.mission_control.core.database import (
            Agent as AgentModel,
        )

        try:
            async with AsyncSessionLocal() as session:
                stmt = select(AgentModel).where(AgentModel.name == self.name)
                result = await session.execute(stmt)
                agent = result.scalar_one_or_none()
                if agent:
                    agent.last_heartbeat = datetime.now(timezone.utc)
                    agent.status = "active"
                    activity = Activity(
                        type=ActivityType.AGENT_HEARTBEAT,
                        agent_id=agent.id,
                        message=f"{self.name} heartbeat",
                    )
                    session.add(activity)
                    await session.commit()
                    self.logger.debug("Recorded heartbeat in DB")
        except Exception as e:
            self.logger.warning("Failed to record heartbeat", error=str(e))

    async def heartbeat(self) -> str:
        """
        Run all health checks. Called by the scheduler every hour.

        Returns:
            Summary string of check results
        """
        self.logger.info("Vision Healer check starting")
        start = datetime.now(timezone.utc)

        await self._record_heartbeat()

        results = await run_all_checks()

        failures = [r for r in results if not r.passed]
        fixes = [r for r in failures if r.fix_applied]
        duration = (datetime.now(timezone.utc) - start).total_seconds()

        # Log summary
        self.logger.info(
            "Health check complete",
            total=len(results),
            passed=len(results) - len(failures),
            failed=len(failures),
            fixes_applied=len(fixes),
            duration_seconds=round(duration, 1),
        )

        # Notify human if any fixes were applied or critical issues found
        critical = [r for r in failures if r.severity == "critical"]

        if fixes or critical:
            await self._report_to_human(results, failures, fixes, duration)

        summary = self._format_summary(results, failures, fixes, duration)
        return summary

    async def _report_to_human(
        self,
        all_results: list[HealthCheckResult],
        failures: list[HealthCheckResult],
        fixes: list[HealthCheckResult],
        duration: float,
    ):
        """Send alert to human about issues found/fixed."""
        lines = []

        for r in failures:
            prefix = "🔧" if r.fix_applied else "❌"
            line = f"{prefix} **{r.name}**: {r.message}"
            if r.fix_applied:
                line += f"\n  → Fix: {r.fix_applied}"
            lines.append(line)

        title = f"{len(failures)} issue(s) found, {len(fixes)} auto-fixed"
        details = "\n".join(lines)

        max_severity = "critical" if any(
            r.severity == "critical" for r in failures
        ) else "warning"

        await notify_human(title, details, severity=max_severity)

    def _format_summary(
        self,
        all_results: list[HealthCheckResult],
        failures: list[HealthCheckResult],
        fixes: list[HealthCheckResult],
        duration: float,
    ) -> str:
        """Format a summary string for the scheduler log."""
        if not failures:
            return f"HEALER_OK: {len(all_results)} checks passed ({duration:.1f}s)"

        fix_names = [r.name for r in fixes]
        fail_names = [r.name for r in failures if not r.fix_applied]
        parts = []
        if fix_names:
            parts.append(f"fixed: {','.join(fix_names)}")
        if fail_names:
            parts.append(f"unresolved: {','.join(fail_names)}")
        return f"HEALER_ALERT: {len(failures)} issues ({'; '.join(parts)}) ({duration:.1f}s)"

    async def run_copilot_fix(self, description: str, working_dir: str) -> Optional[str]:
        """
        Shell out to Copilot CLI with GPT-4.1 for code fixes.

        Uses the same model as the rest of Mission Control (free tier).
        Only called when a health check identifies a code-level issue
        that can't be fixed by DB updates or service restarts.
        """
        from agents.config import settings

        model = settings.copilot_model  # default: gpt-4.1
        self.logger.info("Running Copilot CLI fix", description=description[:100], model=model)

        try:
            proc = await asyncio.create_subprocess_exec(
                "copilot", "-p", description,
                "--model", model,
                "--yes",
                cwd=working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=300,  # 5 min max
            )

            output = stdout.decode() if stdout else ""
            if proc.returncode == 0:
                self.logger.info("Copilot fix completed", output_len=len(output))
                return output
            else:
                err = stderr.decode() if stderr else ""
                self.logger.error("Copilot fix failed", returncode=proc.returncode, stderr=err[:500])
                return None

        except asyncio.TimeoutError:
            self.logger.error("Copilot fix timed out")
            return None
        except Exception as e:
            self.logger.error("Copilot fix error", error=str(e))
            return None


def create_vision_healer() -> VisionHealer:
    """Factory function for the scheduler."""
    return VisionHealer()
