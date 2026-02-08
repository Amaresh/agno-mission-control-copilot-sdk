"""
Friday - Developer Agent

Handles coding tasks, bug fixes, and PR management.
"""

from typing import Optional

import structlog
from sqlalchemy import select

from agents.mission_control.core.base_agent import BaseAgent
from agents.mission_control.core.database import (
    AsyncSessionLocal,
    Task,
    TaskStatus,
    TaskAssignment,
    Agent as AgentModel,
    Notification,
)

logger = structlog.get_logger()


class FridayAgent(BaseAgent):
    """
    Friday - The Developer.

    Responsibilities:
    - Implement features and fixes
    - Code review
    - PR management
    - Technical documentation
    """

    def __init__(self):
        super().__init__(
            name="Friday",
            role="Developer",
            session_key="agent:friday:main",
            mcp_servers=["github"],
            heartbeat_offset=2,
        )

    async def _check_for_work(self) -> Optional[dict]:
        """Check for pending development work."""
        async with AsyncSessionLocal() as session:
            # Get Friday's agent record
            stmt = select(AgentModel).where(AgentModel.name == "Friday")
            result = await session.execute(stmt)
            agent = result.scalar_one_or_none()

            if not agent:
                return None

            # Check for notifications
            stmt = select(Notification).where(
                Notification.mentioned_agent_id == agent.id,
                Notification.delivered == False,
            ).order_by(Notification.created_at.asc()).limit(3)

            result = await session.execute(stmt)
            notifications = result.scalars().all()

            if notifications:
                return {
                    "type": "notifications",
                    "items": [
                        {"id": str(n.id), "content": n.content}
                        for n in notifications
                    ]
                }

            # Resume IN_PROGRESS task if one exists (prevents deadlock)
            stmt = (
                select(Task)
                .join(TaskAssignment, TaskAssignment.task_id == Task.id)
                .where(
                    TaskAssignment.agent_id == agent.id,
                    Task.status == TaskStatus.IN_PROGRESS,
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            in_progress = result.scalar_one_or_none()
            if in_progress:
                return {
                    "type": "task",
                    "task_id": str(in_progress.id),
                    "title": in_progress.title,
                    "description": in_progress.description,
                    "status": "in_progress",
                }

            # Pick up next assigned task
            stmt = (
                select(Task)
                .join(TaskAssignment, TaskAssignment.task_id == Task.id)
                .where(
                    TaskAssignment.agent_id == agent.id,
                    Task.status == TaskStatus.ASSIGNED,
                )
                .order_by(Task.priority.desc(), Task.created_at.asc())
                .limit(1)
            )

            result = await session.execute(stmt)
            task = result.scalar_one_or_none()

            if task:
                return {
                    "type": "task",
                    "task_id": str(task.id),
                    "title": task.title,
                    "description": task.description,
                    "status": task.status.value,
                }

        return None

    async def _do_work(self, work: dict) -> str:
        """Handle pending work."""
        work_type = work.get("type")

        if work_type == "notifications":
            return await self._handle_notifications(work["items"])
        elif work_type == "task":
            return await self._handle_task(work)

        return f"Unknown work type: {work_type}"

    async def _handle_notifications(self, notifications: list[dict]) -> str:
        """Process notifications."""
        for notif in notifications:
            await self.run(
                f"You have a notification: {notif['content']}\n"
                "Please review and respond appropriately."
            )

            # Mark delivered
            async with AsyncSessionLocal() as session:
                from datetime import datetime, timezone
                stmt = select(Notification).where(Notification.id == notif['id'])
                result = await session.execute(stmt)
                notification = result.scalar_one_or_none()
                if notification:
                    notification.delivered = True
                    notification.delivered_at = datetime.now(timezone.utc)
                    await session.commit()

        return f"Processed {len(notifications)} notifications"

    async def _handle_task(self, work: dict) -> str:
        """Work on an assigned task."""
        task_id = work["task_id"]
        title = work["title"]
        description = work["description"]
        status = work["status"]

        # Update status to in_progress if not already
        if status == "assigned":
            async with AsyncSessionLocal() as session:
                stmt = select(Task).where(Task.id == task_id)
                result = await session.execute(stmt)
                task = result.scalar_one_or_none()
                if task:
                    task.status = TaskStatus.IN_PROGRESS
                    await session.commit()

        # Update working memory
        self.update_working_memory(f"""# WORKING.md - Friday

## Current Task

{title}

## Status

In Progress

## Description

{description}

## Next Steps

1. Analyze requirements
2. Implement solution
3. Test
4. Create PR
""")

        # Use agent to work on the task
        response = None
        success = True
        target_branch = "main"
        # Extract repository context from description if present
        repo_line = ""
        if description and "Repository:" in description:
            for line in description.split("\n"):
                if line.strip().startswith("Repository:"):
                    repo_line = line.strip()
                    break
        try:
            repo_context = ""
            repo_name = ""
            if repo_line:
                repo_name = repo_line.replace("Repository:", "").strip()
                repo_context = (
                    f"**Target Repository:** `{repo_name}` on GitHub\n\n"
                )
            # Enforce target repo at the MCP tool level
            self.set_repo_scope(repo_name or None)

            branch_name = f"friday/{task_id[:8]}"
            owner_repo = repo_name.split("/", 1) if "/" in repo_name else ("", "")
            response = await self.run(
                f"Execute the following task by calling GitHub MCP tools NOW. "
                f"Do not explain, plan, or ask questions — just call the tools.\n\n"
                f"Task: {title}\n"
                f"Description: {description}\n\n"
                f"{repo_context}"
                f"STEP 1: Call `create_branch` with owner=\"{owner_repo[0]}\", "
                f"repo=\"{owner_repo[1]}\", branch=\"{branch_name}\", "
                f"from_branch=\"{target_branch}\". If branch already exists, skip to step 2.\n\n"
                f"STEP 2: Call `create_or_update_file` to create deliverable files. "
                f"Use owner=\"{owner_repo[0]}\", repo=\"{owner_repo[1]}\", "
                f"branch=\"{branch_name}\". Write real, complete file content — not placeholders. "
                f"The path is relative to the repo root (e.g. \"src/core/router.py\").\n\n"
                f"STEP 3: Call `create_pull_request` with owner=\"{owner_repo[0]}\", "
                f"repo=\"{owner_repo[1]}\", head=\"{branch_name}\", base=\"{target_branch}\", "
                f"title=\"{title}\".\n\n"
                f"RULES: No local git/shell/filesystem commands. No Copilot skills. "
                f"No update_task_status. Only repo `{repo_name}`. "
                f"Call the first tool now."
            )
        except Exception as e:
            self.logger.error("Agent run failed", error=str(e), task=title[:50])
            response = f"ERROR: {e}"
            success = False
        finally:
            # Clear repo scope
            self.set_repo_scope(None)

            from agents.mission_control.learning.capture import capture_task_outcome
            await capture_task_outcome(
                agent_name=self.name,
                task_id=str(task_id),
                task_title=title,
                from_status="in_progress",
                to_status="review",
                duration_seconds=0.0,
                success=success,
                response_preview=response[:200] if response else None,
                error=response if not success else None,
            )
            # Only transition IN_PROGRESS → REVIEW if an open PR exists
            from agents.mission_control.core.pr_check import (
                extract_target_repo,
                has_open_pr,
            )
            target_repo = extract_target_repo(description)
            pr_found = False
            if target_repo:
                head_prefix = "friday/"
                pr_found, pr_url = await has_open_pr(target_repo, head_prefix)
                if pr_found:
                    self.logger.info("PR verified", pr=pr_url, repo=target_repo)
                else:
                    self.logger.warning(
                        "Task kept IN_PROGRESS — no open PR found",
                        repo=target_repo,
                        head_prefix=head_prefix,
                    )
            else:
                self.logger.warning(
                    "No target repo in description — keeping IN_PROGRESS"
                )

            if pr_found:
                async with AsyncSessionLocal() as session:
                    stmt = select(Task).where(Task.id == task_id)
                    result = await session.execute(stmt)
                    task = result.scalar_one_or_none()
                    if task and task.status == TaskStatus.IN_PROGRESS:
                        task.status = TaskStatus.REVIEW
                        await session.commit()
                        self.logger.info("Task moved to review", task=title[:50])

        return f"Completed: {title}"

    async def create_pull_request(
        self,
        repo: str,
        branch: str,
        title: str,
        body: str,
    ) -> str:
        """Create a pull request."""
        return await self.run(
            f"Create a pull request:\n"
            f"- Repository: {repo}\n"
            f"- Branch: {branch}\n"
            f"- Title: {title}\n"
            f"- Body: {body}\n\n"
            "Use the GitHub MCP create_pull_request tool."
        )


def create_friday() -> FridayAgent:
    """Factory function to create Friday agent."""
    return FridayAgent()
