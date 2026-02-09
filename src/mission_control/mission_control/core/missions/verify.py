"""
VerifyMission — handles REVIEW → DONE | ASSIGNED.

Pure state machine: checks ground truth (PR exists?), transitions state,
records data. Never interprets LLM output.
"""

import uuid as _uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from sqlalchemy import select

from mission_control.mission_control.core.missions.base import BaseMission


class VerifyOutcome(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_APPROVED = "auto_approved"
    SKIPPED = "skipped"


@dataclass
class VerifyResult:
    """Structured result from a single verify execution."""
    task_id: str
    title: str
    outcome: VerifyOutcome
    reason: str
    pr_url: Optional[str] = None


class VerifyMission(BaseMission):
    """REVIEW → DONE (PR found) or REVIEW → ASSIGNED (no PR)."""

    @property
    def _mission_type(self) -> str:
        return "verify"

    async def execute(self) -> VerifyResult:
        """Verify a single task in REVIEW status.

        Returns structured VerifyResult — never a string to parse.
        """
        import time as _time
        from mission_control.mission_control.core.database import (
            AsyncSessionLocal, Task, TaskStatus, Activity, ActivityType,
            TaskAssignment, Agent as AgentModel,
        )
        from mission_control.mission_control.core.pr_check import has_open_pr, has_open_pr_for_task

        task_id = self.task_id if isinstance(self.task_id, _uuid.UUID) else _uuid.UUID(str(self.task_id))
        repo_name = self.repository
        title = self.title
        t0 = _time.monotonic()

        async with AsyncSessionLocal() as session:
            stmt = select(Task).where(Task.id == task_id)
            result = await session.execute(stmt)
            task = result.scalar_one_or_none()
            if not task or task.status != TaskStatus.REVIEW:
                return VerifyResult(
                    task_id=str(task_id), title=title,
                    outcome=VerifyOutcome.SKIPPED,
                    reason="not in REVIEW status",
                )

            # Review tasks don't produce their own PRs — auto-approve via metadata
            if task.mission_type == "review":
                task.status = TaskStatus.DONE
                session.add(Activity(
                    type=ActivityType.TASK_STATUS_CHANGED,
                    task_id=task.id,
                    message="Status: review → done (review task, no PR expected)",
                ))
                await session.commit()
                await self.capture_transition("REVIEW", "DONE", duration_sec=_time.monotonic() - t0)
                from mission_control.mission_control.learning.capture import capture_mission_complete
                await capture_mission_complete(
                    agent_name=self.agent.name,
                    mission_type=self._mission_type,
                    task_id=str(task_id),
                    total_duration_sec=_time.monotonic() - t0,
                    transition_path="REVIEW→DONE(auto-approve)",
                )
                return VerifyResult(
                    task_id=str(task_id), title=title,
                    outcome=VerifyOutcome.AUTO_APPROVED,
                    reason="review task — no PR expected",
                )

            # Check for matching open PR — task_id first, then agent prefix fallback
            short_id = str(task_id)[:8]
            pr_found, pr_url = await has_open_pr_for_task(repo_name, short_id)
            if not pr_found:
                # Fallback: check by assigned agent's branch prefix
                assign_stmt = (
                    select(AgentModel.name)
                    .join(TaskAssignment, TaskAssignment.agent_id == AgentModel.id)
                    .where(TaskAssignment.task_id == task_id)
                )
                assign_result = await session.execute(assign_stmt)
                agent_name = assign_result.scalar_one_or_none()
                if agent_name:
                    pr_found, pr_url = await has_open_pr(
                        repo_name, f"{agent_name.lower()}/",
                    )

            if pr_found:
                self.logger.info("PR found for task", pr=pr_url)
                task.status = TaskStatus.DONE
                session.add(Activity(
                    type=ActivityType.TASK_STATUS_CHANGED,
                    task_id=task.id,
                    message=f"Status: review → done (PR verified: {pr_url})",
                ))
                await session.commit()
                await self.capture_transition(
                    "REVIEW", "DONE",
                    duration_sec=_time.monotonic() - t0,
                    guard="has_open_pr", guard_result=True,
                )
                from mission_control.mission_control.learning.capture import capture_mission_complete
                await capture_mission_complete(
                    agent_name=self.agent.name,
                    mission_type=self._mission_type,
                    task_id=str(task_id),
                    total_duration_sec=_time.monotonic() - t0,
                    transition_path="REVIEW→DONE",
                )
                return VerifyResult(
                    task_id=str(task_id), title=title,
                    outcome=VerifyOutcome.APPROVED,
                    reason="PR verified by gatekeeper",
                    pr_url=pr_url,
                )
            else:
                self.logger.warning("No PR found — rejecting")
                task.status = TaskStatus.ASSIGNED
                session.add(Activity(
                    type=ActivityType.TASK_STATUS_CHANGED,
                    task_id=task.id,
                    message="Status: review → assigned (no matching PR found)",
                ))
                await session.commit()
                await self.capture_transition(
                    "REVIEW", "ASSIGNED",
                    duration_sec=_time.monotonic() - t0,
                    guard="has_open_pr", guard_result=False,
                )
                return VerifyResult(
                    task_id=str(task_id), title=title,
                    outcome=VerifyOutcome.REJECTED,
                    reason="no matching PR found",
                )

    @classmethod
    async def verify_batch(cls, agent, tasks: list[dict]) -> str:
        """Verify a batch of tasks. Returns summary string."""
        done_count = 0
        assigned_count = 0

        for t in tasks:
            tid = t.get("id") or t.get("task_id")
            config = t.get("mission_config", {})
            if not config.get("repository"):
                from mission_control.mission_control.core.pr_check import extract_target_repo
                repo = extract_target_repo(t.get("description", ""))
                if repo:
                    config["repository"] = repo

            mission = cls(
                agent=agent,
                task_id=tid,
                title=t.get("title", ""),
                description=t.get("description", ""),
                mission_config=config,
            )
            result = await mission.execute()
            if result.outcome in (VerifyOutcome.APPROVED, VerifyOutcome.AUTO_APPROVED):
                done_count += 1
            elif result.outcome == VerifyOutcome.REJECTED:
                assigned_count += 1

        return (
            f"Gatekeeper reviewed {len(tasks)} tasks: "
            f"{done_count} approved, {assigned_count} sent back"
        )
