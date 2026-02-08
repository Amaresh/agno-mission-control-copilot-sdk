"""
VerifyMission — handles REVIEW → DONE | ASSIGNED.

Extracted from JarvisAgent._handle_review_tasks().
Single source of truth for PR verification and review gating.
"""

import uuid as _uuid

from sqlalchemy import select

from agents.mission_control.core.missions.base import BaseMission


class VerifyMission(BaseMission):
    """REVIEW → DONE (PR found) or REVIEW → ASSIGNED (no PR)."""

    async def execute(self) -> str:
        """Verify a single task in REVIEW status.

        Called once per task (unlike the old batch method).
        Returns a summary string.
        """
        from agents.mission_control.core.database import (
            AsyncSessionLocal, Task, TaskStatus, Activity, ActivityType,
            TaskAssignment, Agent as AgentModel,
        )
        from agents.mission_control.core.pr_check import has_open_pr, has_open_pr_for_task

        task_id = self.task_id if isinstance(self.task_id, _uuid.UUID) else _uuid.UUID(str(self.task_id))
        repo_name = self.repository
        title = self.title

        async with AsyncSessionLocal() as session:
            stmt = select(Task).where(Task.id == task_id)
            result = await session.execute(stmt)
            task = result.scalar_one_or_none()
            if not task or task.status != TaskStatus.REVIEW:
                return f"Skipped (not in review): {title}"

            # Skip review-of-PR tasks — they don't produce their own PRs
            title_lower = (title or "").lower()
            if any(pat in title_lower for pat in [
                "review pr#", "pr#", "review batch:", "[review pr#",
            ]):
                task.status = TaskStatus.DONE
                session.add(Activity(
                    type=ActivityType.TASK_STATUS_CHANGED,
                    task_id=task.id,
                    message="Status: review → done (review task, no PR expected)",
                ))
                await session.commit()
                return f"Auto-approved (review task): {title}"

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
                    message="Status: review → done (PR verified by gatekeeper)",
                ))
                await session.commit()
                return f"Approved: {title}"
            else:
                self.logger.warning("No PR found — rejecting")
                task.status = TaskStatus.ASSIGNED
                session.add(Activity(
                    type=ActivityType.TASK_STATUS_CHANGED,
                    task_id=task.id,
                    message="Status: review → assigned (no matching PR found)",
                ))
                await session.commit()
                return f"Rejected (no PR): {title}"

    @classmethod
    async def verify_batch(cls, agent, tasks: list[dict]) -> str:
        """Convenience method: verify a batch of tasks. Returns summary."""
        done_count = 0
        assigned_count = 0

        for t in tasks:
            tid = t.get("id") or t.get("task_id")
            config = t.get("mission_config", {})
            # Fallback: extract repo from description if no config
            if not config.get("repository"):
                from agents.mission_control.core.pr_check import extract_target_repo
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
            if "Approved" in result or "Auto-approved" in result:
                done_count += 1
            elif "Rejected" in result:
                assigned_count += 1

        return (
            f"Gatekeeper reviewed {len(tasks)} tasks: "
            f"{done_count} approved, {assigned_count} sent back"
        )
