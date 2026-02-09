"""
Vision Healer Agent — deterministic system health monitor.

Does NOT use Copilot SDK or LLM for health checks. Runs a checklist
of deterministic checks every hour. For code fixes, shells out to
`copilot` CLI with GPT-4.1 (free tier GitHub model).

Reports issues via Telegram + GitHub Issue.
"""

import asyncio
import subprocess
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select

from mission_control.squad.vision.checks import run_all_checks, HealthCheckResult
from mission_control.squad.vision.notify import notify_human

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
        from mission_control.mission_control.core.database import (
            AsyncSessionLocal,
            Agent as AgentModel,
            Activity,
            ActivityType,
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
                else:
                    self.logger.warning(
                        "Agent row not found in DB — heartbeat not recorded. "
                        "Run `mc seed-agents` to create the agent record.",
                        agent_name=self.name,
                    )
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

        # Record heartbeat FIRST so watchdog knows Vision is alive,
        # even if health checks are slow or timeout.
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
        from mission_control.config import settings

        model = settings.vision_model  # separate config for Vision agent
        self.logger.info("Running Copilot CLI fix", description=description[:100], model=model)

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "copilot", "-p", description,
                "--model", model,
                "--allow-all",
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
            self.logger.error("Copilot fix timed out — killing subprocess")
            if proc:
                proc.kill()
                await proc.wait()
            return None
        except Exception as e:
            self.logger.error("Copilot fix error", error=str(e))
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return None

    # ------------------------------------------------------------------
    # Telegram escalation: execute human commands during crises
    # ------------------------------------------------------------------

    @staticmethod
    def _get_project_root() -> str:
        """Return the mission-control repo root (4 dirnames from this file)."""
        import os
        return os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )))

    async def execute_command(self, user_message: str) -> str:
        """
        Execute a human admin command via Copilot CLI (Opus 4.6).

        Used when Telegram escalates from Jarvis → Vision.  The human
        can send literal shell/gh commands or natural-language ops
        instructions.  Vision interprets and executes via ``copilot``
        CLI with ``--allow-all``.

        Returns the Copilot CLI output (stdout), or an error string.
        """
        from mission_control.config import settings

        model = settings.vision_model
        project_root = self._get_project_root()

        prompt = (
            "You are Vision, an ops agent for Mission Control. "
            "The human admin is sending you a command via Telegram during a crisis. "
            "Rules:\n"
            "- If the message looks like a shell/gh/git command, execute it LITERALLY "
            "and show the full output.\n"
            "- If it's natural language, interpret it as an ops instruction and execute "
            "using gh CLI, git, systemctl --user, python3, psql, or whatever tools are needed.\n"
            "- Be thorough — the goal is to unblock, however hard it is.\n"
            "- Always show command output so the human can verify.\n"
            "- You are running from the mission-control repo root: " + project_root + "\n"
            "- Key paths: agents/ (source), tests/, infra/, docs/, pyproject.toml\n"
            "- The review repo is Amaresh/mission-control-review on GitHub.\n"
            "- The main repo is Amaresh/mission-control on GitHub.\n"
            "- Database is PostgreSQL (connection via DATABASE_URL env var).\n"
            "- Services are systemd --user units: mc-bot, mc-scheduler, mc-mcp, mc-api.\n"
            "- Use 'git --no-pager' to avoid pager hangs.\n\n"
            "Human command:\n" + user_message
        )

        self.logger.info(
            "Vision execute_command",
            message_preview=user_message[:100],
            model=model,
            cwd=project_root,
        )

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "copilot", "-p", prompt,
                "--model", model,
                "--allow-all",
                cwd=project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=300,
            )

            output = stdout.decode() if stdout else ""
            if proc.returncode == 0 and output.strip():
                self.logger.info("Vision command completed", output_len=len(output))
                return output.strip()
            else:
                err = stderr.decode() if stderr else ""
                self.logger.error(
                    "Vision command failed",
                    returncode=proc.returncode,
                    stderr=err[:500],
                )
                return f"⚠️ Command finished with exit code {proc.returncode}.\n{err[:1000]}" if err else \
                       f"⚠️ Command finished with exit code {proc.returncode} (no output)."

        except asyncio.TimeoutError:
            self.logger.error("Vision command timed out (5 min) — killing subprocess")
            if proc:
                proc.kill()
                await proc.wait()
            return "⏰ Command timed out after 5 minutes. The subprocess has been killed."
        except Exception as e:
            self.logger.error("Vision command error", error=str(e))
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return f"❌ Vision error: {str(e)}"

    async def quick_health_check(self) -> bool:
        """Fast health check — are services running and no critical stale tasks?

        Used by Telegram bot to decide whether to suggest handback to Jarvis.
        """
        try:
            from mission_control.squad.vision.checks import (
                check_service_health,
                check_stale_tasks,
            )

            svc_results = await check_service_health()
            stale_results = await check_stale_tasks()

            svc_ok = all(r.passed for r in svc_results)
            stale_ok = all(r.passed for r in stale_results)
            return svc_ok and stale_ok
        except Exception:
            return False


def create_vision_healer() -> VisionHealer:
    """Factory function for the scheduler."""
    return VisionHealer()
