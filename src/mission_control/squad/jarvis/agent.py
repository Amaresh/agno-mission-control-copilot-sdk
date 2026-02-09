"""
Jarvis - Squad Lead / Coordinator Agent

Primary interface with humans via Telegram.
Coordinates task distribution across the agent squad.
"""

from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select

from mission_control.mission_control.core.base_agent import BaseAgent
from mission_control.mission_control.core.database import (
    Activity,
    ActivityType,
    AsyncSessionLocal,
    Notification,
    Task,
    TaskAssignment,
    TaskStatus,
)
from mission_control.mission_control.core.database import (
    Agent as AgentModel,
)

logger = structlog.get_logger()


class JarvisAgent(BaseAgent):
    """
    Jarvis - The Squad Lead.

    Responsibilities:
    - Receive tasks from humans via Telegram
    - Delegate to appropriate specialists
    - Monitor team progress
    - Report status updates
    """

    def __init__(self):
        super().__init__(
            name="Jarvis",
            role="Squad Lead / Coordinator",
            level="lead",
            session_key="agent:jarvis:main",
            mcp_servers=["github"],  # GitHub MCP only (telegram MCP needs API_ID/API_HASH)
            heartbeat_offset=0,
        )

    async def _check_for_work(self) -> Optional[dict]:
        """Check for pending work during heartbeat."""
        async with AsyncSessionLocal() as session:
            # Get this agent's record for filtering
            stmt = select(AgentModel).where(AgentModel.name == self.name)
            result = await session.execute(stmt)
            agent = result.scalar_one_or_none()
            if not agent:
                return None

            # 1. Check for undelivered notifications for THIS agent only
            stmt = select(Notification).where(
                Notification.mentioned_agent_id == agent.id,
                Notification.delivered == False
            ).order_by(Notification.created_at.asc()).limit(5)

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

            # 2. Check for tasks needing review — skip if there's already
            # a pending (undelivered) review notification to avoid duplicates
            stmt = select(Notification).where(
                Notification.delivered == False,
                Notification.content.ilike("%review%"),
            ).limit(1)
            result = await session.execute(stmt)
            pending_review_notif = result.scalar_one_or_none()

            if not pending_review_notif:
                stmt = select(Task).where(
                    Task.status == TaskStatus.REVIEW
                ).order_by(Task.updated_at.asc()).limit(5)

                result = await session.execute(stmt)
                review_tasks = result.scalars().all()

                if review_tasks:
                    return {
                        "type": "review",
                        "items": [
                            {"id": str(t.id), "title": t.title, "description": t.description or ""}
                            for t in review_tasks
                        ]
                    }

            # 3. Check for tasks assigned to Jarvis (IN_PROGRESS first, then ASSIGNED)
            for status in [TaskStatus.IN_PROGRESS, TaskStatus.ASSIGNED]:
                stmt = (
                    select(Task)
                    .join(TaskAssignment, TaskAssignment.task_id == Task.id)
                    .where(
                        TaskAssignment.agent_id == agent.id,
                        Task.status == status,
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

            # 4. Check for blocked tasks
            stmt = select(Task).where(
                Task.status == TaskStatus.BLOCKED
            ).order_by(Task.updated_at.asc()).limit(5)

            result = await session.execute(stmt)
            blocked_tasks = result.scalars().all()

            if blocked_tasks:
                return {
                    "type": "blocked",
                    "items": [
                        {"id": str(t.id), "title": t.title}
                        for t in blocked_tasks
                    ]
                }

        return None

    async def _do_work(self, work: dict) -> str:
        """Handle pending work."""
        work_type = work.get("type")
        items = work.get("items", [])

        if work_type == "notifications":
            return await self._handle_notifications(items)
        elif work_type == "review":
            return await self._handle_review_tasks(items)
        elif work_type == "task":
            return await self._handle_task(work)
        elif work_type == "blocked":
            return await self._handle_blocked_tasks(items)

        return f"Unknown work type: {work_type}"

    async def _handle_task(self, work: dict) -> str:
        """Decompose task into subtasks and delegate to workers. Jarvis never executes."""
        task_id = work["task_id"]
        title = work["title"]
        description = work["description"]
        status = work["status"]

        # Transition to IN_PROGRESS
        if status == "assigned":
            async with AsyncSessionLocal() as session:
                task = (await session.execute(
                    select(Task).where(Task.id == task_id)
                )).scalar_one_or_none()
                if task:
                    task.status = TaskStatus.IN_PROGRESS
                    session.add(Activity(
                        type=ActivityType.TASK_STATUS_CHANGED,
                        agent_id=(await session.execute(
                            select(AgentModel.id).where(AgentModel.name == self.name)
                        )).scalar(),
                        task_id=task.id,
                        message="Status: assigned → in_progress",
                    ))
                    await session.commit()

        # Extract repository context
        repo_line = ""
        if description and "Repository:" in description:
            for line in description.split("\n"):
                if line.strip().startswith("Repository:"):
                    repo_line = line.strip()
                    break

        # Build real-time workload for load-balanced assignment
        workload_lines = []
        try:
            async with AsyncSessionLocal() as session:
                from sqlalchemy import func as sqlfunc
                agents = (await session.execute(
                    select(AgentModel).where(AgentModel.name != "Jarvis").order_by(AgentModel.name)
                )).scalars().all()
                for ag in agents:
                    task_count = (await session.execute(
                        select(sqlfunc.count()).select_from(TaskAssignment)
                        .join(Task, Task.id == TaskAssignment.task_id)
                        .where(TaskAssignment.agent_id == ag.id, Task.status != TaskStatus.DONE)
                    )).scalar() or 0
                    role_tag = "developer"
                    role_lower = (ag.role or "").lower()
                    if "doc" in role_lower or "plan" in role_lower:
                        role_tag = "planner/docs"
                    elif "test" in role_lower or "qa" in role_lower:
                        role_tag = "testing/QA"
                    elif "review" in role_lower:
                        role_tag = "reviewer (do NOT assign dev tasks)"
                    elif "healer" in role_lower or "monitor" in role_lower or "ops" in role_lower:
                        role_tag = "ops/healer (do NOT assign dev tasks)"
                    elif "infra" in role_lower:
                        role_tag = "infrastructure ops"
                    workload_lines.append(f"     - **{ag.name}** ({role_tag}) — {task_count} active tasks")
        except Exception:
            # Fallback: static list if DB query fails
            workload_lines = [
                "     - **Friday, Loki, Quill, Pepper, Fury, Wanda** — developers",
                "     - **Wong** — planner/docs",
                "     - **Shuri** — testing/QA",
            ]
        workload_str = "\n".join(workload_lines)

        response = None
        success = True
        try:
            response = await self.run(
                f"You have been assigned a task to DECOMPOSE and DELEGATE.\n\n"
                f"**Title:** {title}\n\n"
                f"**Description:** {description}\n\n"
                f"## Your Job (MANDATORY)\n"
                f"You are the coordinator. You do NOT execute this task yourself.\n"
                f"Instead you MUST:\n\n"
                f"1. **Analyze** the task and break it into 2-5 concrete subtasks\n"
                f"2. **Create each subtask** using the `create_task` tool with:\n"
                f"   - A clear, specific title\n"
                f"   - Detailed description of exactly what to deliver\n"
                f"   - `repository`: `{repo_line.replace('Repository:', '').strip() or 'ASK THE HUMAN'}`\n"
                f"   - `assignees`: **LOAD-BALANCE** — assign to agents with FEWEST active tasks first:\n"
                f"{workload_str}\n"
                f"     **RULE: Prefer agents with 0 active tasks. NEVER pile tasks on one agent when others are free.**\n"
                f"3. **Mark this parent task done** using `update_task_status` with status='done'\n\n"
                f"DO NOT write code. DO NOT create branches. DO NOT open PRs.\n"
                f"Your only output is subtasks assigned to workers.\n"
            )
        except Exception as e:
            self.logger.error("Task decomposition failed", error=str(e), task=title[:50])
            response = f"ERROR: {e}"
            success = False
        finally:
            from mission_control.mission_control.learning.capture import capture_task_outcome
            await capture_task_outcome(
                agent_name=self.name,
                task_id=str(task_id),
                task_title=title,
                from_status="in_progress",
                to_status="done",
                duration_seconds=0.0,
                success=success,
                response_preview=response[:200] if response else None,
                error=response if not success else None,
            )
            # Mark parent task done (subtasks are now tracked independently)
            async with AsyncSessionLocal() as session:
                task = (await session.execute(
                    select(Task).where(Task.id == task_id)
                )).scalar_one_or_none()
                if task and task.status == TaskStatus.IN_PROGRESS:
                    task.status = TaskStatus.DONE
                    await session.commit()
                    self.logger.info("Parent task decomposed and closed", task=title[:50])

        return f"Decomposed and delegated: {title}"

    async def _handle_notifications(self, notifications: list[dict]) -> str:
        """Process pending notifications."""
        results = []

        for notif in notifications:
            # Use the agent to process the notification
            await self.run(
                f"You have a notification: {notif['content']}\n\n"
                "Please review and take appropriate action."
            )
            results.append(f"Handled: {notif['content'][:50]}...")

            # Mark as delivered
            async with AsyncSessionLocal() as session:
                stmt = select(Notification).where(
                    Notification.id == notif['id']
                )
                result = await session.execute(stmt)
                notification = result.scalar_one_or_none()
                if notification:
                    notification.delivered = True
                    notification.delivered_at = datetime.now(timezone.utc)
                    await session.commit()

        return f"Processed {len(results)} notifications"

    async def _handle_review_tasks(self, tasks: list[dict]) -> str:
        """ProofOfWork Gatekeeper — delegates to VerifyMission."""
        from mission_control.mission_control.core.missions.verify import VerifyMission
        return await VerifyMission.verify_batch(self, tasks)

    async def _handle_blocked_tasks(self, tasks: list[dict]) -> str:
        """Handle blocked tasks."""
        task_list = "\n".join([f"- {t['title']}" for t in tasks])

        await self.run(
            f"The following tasks are blocked:\n{task_list}\n\n"
            "Please analyze what's blocking each task and either:\n"
            "1. Unblock it yourself if possible\n"
            "2. Notify the human via Telegram if human intervention needed"
        )

        return f"Analyzed {len(tasks)} blocked tasks"

    async def create_task(
        self,
        title: str,
        description: str,
        assignees: list[str],
        priority: str = "medium",
    ) -> str:
        """Create a new task and assign to ONE agent (first in list)."""
        from mission_control.mission_control.core.database import (
            Activity,
            ActivityType,
            Task,
            TaskAssignment,
            TaskPriority,
            TaskStatus,
        )

        # Enforce single assignee
        assignee_name = assignees[0] if assignees else None

        async with AsyncSessionLocal() as session:
            task = Task(
                title=title,
                description=description,
                status=TaskStatus.ASSIGNED if assignee_name else TaskStatus.INBOX,
                priority=TaskPriority(priority),
            )
            session.add(task)
            await session.flush()

            activity = Activity(
                type=ActivityType.TASK_CREATED,
                task_id=task.id,
                message=f"Created task: {title}",
            )
            session.add(activity)

            if assignee_name:
                stmt = select(AgentModel).where(
                    AgentModel.name.ilike(assignee_name)
                )
                result = await session.execute(stmt)
                agent = result.scalar_one_or_none()

                if agent:
                    assignment = TaskAssignment(
                        task_id=task.id,
                        agent_id=agent.id,
                    )
                    session.add(assignment)

                    notif = Notification(
                        mentioned_agent_id=agent.id,
                        content=f"You have been assigned: {title}",
                    )
                    session.add(notif)

            await session.commit()

            self.logger.info(
                "Created task",
                task_id=str(task.id),
                title=title,
                assignee=assignee_name,
            )

            return str(task.id)

    async def send_telegram_message(self, message: str) -> str:
        """Send a message to the human via Telegram."""
        return await self.run(
            f"Please send this message to the human via Telegram:\n\n{message}"
        )

    async def generate_daily_standup(self) -> str:
        """Generate daily standup summary."""
        from mission_control.mission_control.core.database import Task

        async with AsyncSessionLocal() as session:
            # Get today's activities
            today = datetime.now(timezone.utc).date()

            # Completed tasks
            stmt = select(Task).where(
                Task.status == TaskStatus.DONE,
                Task.updated_at >= datetime(today.year, today.month, today.day)
            )
            result = await session.execute(stmt)
            completed = result.scalars().all()

            # In progress
            stmt = select(Task).where(
                Task.status == TaskStatus.IN_PROGRESS
            )
            result = await session.execute(stmt)
            in_progress = result.scalars().all()

            # Blocked
            stmt = select(Task).where(
                Task.status == TaskStatus.BLOCKED
            )
            result = await session.execute(stmt)
            blocked = result.scalars().all()

            # Review
            stmt = select(Task).where(
                Task.status == TaskStatus.REVIEW
            )
            result = await session.execute(stmt)
            review = result.scalars().all()

        standup = f"""📊 DAILY STANDUP — {today.strftime('%b %d, %Y')}

✅ COMPLETED TODAY ({len(completed)})
{chr(10).join([f'• {t.title}' for t in completed]) or '• None'}

🔄 IN PROGRESS ({len(in_progress)})
{chr(10).join([f'• {t.title}' for t in in_progress]) or '• None'}

🚫 BLOCKED ({len(blocked)})
{chr(10).join([f'• {t.title}' for t in blocked]) or '• None'}

👀 NEEDS REVIEW ({len(review)})
{chr(10).join([f'• {t.title}' for t in review]) or '• None'}
"""
        return standup


def create_jarvis() -> JarvisAgent:
    """Factory function to create Jarvis agent."""
    return JarvisAgent()
