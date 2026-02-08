"""
Agent factory - creates and manages agent instances.
"""

from datetime import datetime, timezone
from typing import Optional

import structlog

from agents.mission_control.core.base_agent import BaseAgent

logger = structlog.get_logger()


# Agent registry - MCP servers mapped to available npm packages
AGENT_CONFIGS = {
    "jarvis": {
        "name": "Jarvis",
        "role": "Squad Lead / Coordinator",
        "session_key": "agent:jarvis:main",
        "mcp_servers": ["github", "telegram"],
        "heartbeat_offset": 0,
        "level": "lead",
    },
    "friday": {
        "name": "Friday",
        "role": "Developer",
        "session_key": "agent:friday:main",
        "mcp_servers": ["github"],
        "heartbeat_offset": 2,
        "level": "specialist",
    },
    "vision": {
        "name": "Vision",
        "role": "System Healer / Ops Monitor",
        "session_key": "agent:vision:main",
        "mcp_servers": [],
        "heartbeat_offset": 0,
        "level": "lead",
        "agent_class": "healer",
    },
    "wong": {
        "name": "Wong",
        "role": "Documentation",
        "session_key": "agent:wong:main",
        "mcp_servers": ["github"],
        "heartbeat_offset": 6,
        "level": "specialist",
    },
    "shuri": {
        "name": "Shuri",
        "role": "Testing & QA",
        "session_key": "agent:shuri:main",
        "mcp_servers": ["github"],
        "heartbeat_offset": 8,
        "level": "specialist",
    },
    "fury": {
        "name": "Fury",
        "role": "Developer",
        "session_key": "agent:fury:main",
        "mcp_servers": ["github"],
        "heartbeat_offset": 10,
        "level": "specialist",
    },
    "pepper": {
        "name": "Pepper",
        "role": "Developer",
        "session_key": "agent:pepper:main",
        "mcp_servers": ["github"],
        "heartbeat_offset": 12,
        "level": "specialist",
    },
    "loki": {
        "name": "Loki",
        "role": "Developer",
        "session_key": "agent:loki:main",
        "mcp_servers": ["github"],
        "heartbeat_offset": 14,
        "level": "specialist",
    },
    "quill": {
        "name": "Quill",
        "role": "Infrastructure Ops — DigitalOcean Monitor",
        "session_key": "agent:quill:main",
        "mcp_servers": ["digitalocean"],
        "heartbeat_offset": 16,
        "level": "specialist",
    },
    "wanda": {
        "name": "Wanda",
        "role": "Developer",
        "session_key": "agent:wanda:main",
        "mcp_servers": ["github"],
        "heartbeat_offset": 18,
        "level": "specialist",
    },
}


class GenericAgent(BaseAgent):
    """Generic agent implementation for squad members."""

    async def _send_telegram_notification(self, content: str):
        """Send a notification directly to Telegram instead of relying on the LLM."""
        from agents.config import settings
        chat_id = settings.telegram_chat_id
        bot_token = settings.telegram_bot_token
        if not chat_id or not bot_token:
            self.logger.warning("No Telegram credentials, skipping notification")
            return
        import httpx
        text = f"📬 *{self.name}*\n\n{content}"
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                    timeout=10,
                )
            self.logger.info("Sent Telegram notification")
        except Exception as e:
            self.logger.error("Telegram notification failed", error=str(e))

    async def _check_for_work(self) -> Optional[dict]:
        """Check for pending work during heartbeat."""
        from sqlalchemy import select

        from agents.mission_control.core.database import (
            Agent as AgentModel,
        )
        from agents.mission_control.core.database import (
            AsyncSessionLocal,
            Notification,
            Task,
            TaskAssignment,
            TaskStatus,
        )

        async with AsyncSessionLocal() as session:
            # Get agent record
            stmt = select(AgentModel).where(AgentModel.name == self.name)
            result = await session.execute(stmt)
            agent = result.scalar_one_or_none()

            if not agent:
                return None

            # Check notifications
            stmt = select(Notification).where(
                Notification.mentioned_agent_id == agent.id,
                not Notification.delivered,
            ).limit(3)

            result = await session.execute(stmt)
            notifications = result.scalars().all()

            if notifications:
                return {
                    "type": "notifications",
                    "items": [{"id": str(n.id), "content": n.content} for n in notifications]
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

            # Check assigned tasks — single assignee per task enforced at assignment time
            stmt = (
                select(Task)
                .join(TaskAssignment, TaskAssignment.task_id == Task.id)
                .where(
                    TaskAssignment.agent_id == agent.id,
                    Task.status == TaskStatus.ASSIGNED,
                )
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
                }

            # Lead agents also review tasks in REVIEW status
            if self.level == "lead":
                stmt = (
                    select(Task)
                    .where(Task.status == TaskStatus.REVIEW)
                    .limit(5)
                )
                result = await session.execute(stmt)
                review_tasks = result.scalars().all()
                if review_tasks:
                    return {
                        "type": "review_tasks",
                        "tasks": [
                            {"title": t.title, "task_id": str(t.id), "description": t.description or ""}
                            for t in review_tasks
                        ],
                    }

        return None

    async def _do_work(self, work: dict) -> str:
        """Handle pending work."""
        from sqlalchemy import select

        from agents.mission_control.core.database import (
            Activity,
            ActivityType,
            AsyncSessionLocal,
            Notification,
            Task,
            TaskStatus,
        )
        from agents.mission_control.core.database import (
            Agent as AgentModel,
        )

        work_type = work.get("type")

        if work_type == "notifications":
            for notif in work["items"]:
                content = notif['content']
                # If the notification asks to notify the human via Telegram,
                # send it directly instead of asking the LLM (which can't send messages)
                if "notify the human via telegram" in content.lower():
                    await self._send_telegram_notification(content)
                else:
                    await self.run(f"Handle notification: {content}")
            # Mark notifications as delivered
            async with AsyncSessionLocal() as session:
                for notif in work["items"]:
                    stmt = select(Notification).where(
                        Notification.id == notif['id']
                    )
                    result = await session.execute(stmt)
                    notification = result.scalar_one_or_none()
                    if notification:
                        notification.delivered = True
                        notification.delivered_at = datetime.now(timezone.utc)
                await session.commit()
            return f"Processed {len(work['items'])} notifications"

        elif work_type == "task":
            task_id = work.get("task_id")
            title = work.get("title", "")
            description = work.get("description", "")

            # Transition ASSIGNED → IN_PROGRESS
            async with AsyncSessionLocal() as session:
                stmt = select(Task).where(Task.id == task_id)
                result = await session.execute(stmt)
                task = result.scalar_one_or_none()
                if task and task.status == TaskStatus.ASSIGNED:
                    task.status = TaskStatus.IN_PROGRESS
                    activity = Activity(
                        type=ActivityType.TASK_STATUS_CHANGED,
                        agent_id=task_id,  # will be set below
                        task_id=task.id,
                        message="Status: assigned → in_progress",
                    )
                    # Get agent id for activity
                    agent_result = await session.execute(
                        select(AgentModel).where(AgentModel.name == self.name)
                    )
                    agent_record = agent_result.scalar_one_or_none()
                    if agent_record:
                        activity.agent_id = agent_record.id
                    session.add(activity)
                    await session.commit()

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

                branch_name = f"{self.name.lower()}/{task_id[:8]}"
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
                    f"branch=\"{branch_name}\". Write real, complete implementation code — "
                    f"NOT plan documents, NOT outlines, NOT markdown breakdowns. "
                    f"Deliver .py/.js/.ts/.yaml files with working logic. "
                    f"The path is relative to the repo root (e.g. \"src/monitoring/health.py\").\n\n"
                    f"STEP 3: Call `create_pull_request` with owner=\"{owner_repo[0]}\", "
                    f"repo=\"{owner_repo[1]}\", head=\"{branch_name}\", base=\"{target_branch}\", "
                    f"title=\"{title}\".\n\n"
                    f"RULES: No local git/shell/filesystem commands. No Copilot skills. "
                    f"No update_task_status. No plan/outline/breakdown .md files. Only repo `{repo_name}`. "
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
                    head_prefix = f"{self.name.lower()}/"
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
                            agent_result = await session.execute(
                                select(AgentModel).where(AgentModel.name == self.name)
                            )
                            agent_record = agent_result.scalar_one_or_none()
                            msg = "Status: in_progress → review"
                            if response:
                                msg += f". Agent output: {response[:200]}"
                            activity = Activity(
                                type=ActivityType.TASK_STATUS_CHANGED,
                                agent_id=agent_record.id if agent_record else task_id,
                                task_id=task.id,
                                message=msg,
                            )
                            session.add(activity)
                            await session.commit()
                            self.logger.info("Task moved to review", task=title[:50])

            return f"Completed: {title}"

        elif work_type == "review_tasks":
            tasks = work.get("tasks", [])
            import uuid as _uuid

            from agents.mission_control.core.pr_check import (
                extract_target_repo,
                has_open_pr_for_task,
            )

            done_count = 0
            assigned_count = 0

            async with AsyncSessionLocal() as session:
                for t in tasks:
                    tid = _uuid.UUID(t["task_id"])
                    desc = t.get("description", "")
                    title = t["title"]

                    stmt = select(Task).where(Task.id == tid)
                    result = await session.execute(stmt)
                    task = result.scalar_one_or_none()
                    if not task or task.status != TaskStatus.REVIEW:
                        continue

                    target_repo = extract_target_repo(desc)
                    pr_found = False
                    if target_repo:
                        short_id = str(tid)[:8]
                        pr_found, pr_url = await has_open_pr_for_task(target_repo, short_id)
                        if pr_found:
                            self.logger.info("PR found for task", task=title[:50], pr=pr_url)

                    if pr_found:
                        task.status = TaskStatus.DONE
                        activity = Activity(
                            type=ActivityType.TASK_STATUS_CHANGED,
                            task_id=task.id,
                            message="Status: review → done (PR verified by reviewer)",
                        )
                        session.add(activity)
                        done_count += 1
                        self.logger.info("Task approved", task=title[:50])
                    else:
                        task.status = TaskStatus.ASSIGNED
                        activity = Activity(
                            type=ActivityType.TASK_STATUS_CHANGED,
                            task_id=task.id,
                            message="Status: review → assigned (no matching PR found)",
                        )
                        session.add(activity)
                        assigned_count += 1
                        self.logger.warning("Task rejected — no PR", task=title[:50])

                await session.commit()

            return (
                f"Reviewed {len(tasks)} tasks: "
                f"{done_count} approved, {assigned_count} sent back"
            )

        return "HEARTBEAT_OK"


class AgentFactory:
    """Factory for creating agent instances."""

    _instances: dict[str, BaseAgent] = {}

    @classmethod
    def get_agent(cls, name: str) -> BaseAgent:
        """Get or create an agent instance."""
        key = name.lower()

        if key not in cls._instances:
            if key not in AGENT_CONFIGS:
                raise ValueError(f"Unknown agent: {name}")

            config = AGENT_CONFIGS[key]

            # Use specialized agents for Jarvis and Friday
            if key == "jarvis":
                from agents.squad.jarvis.agent import JarvisAgent
                cls._instances[key] = JarvisAgent()
            elif key == "friday":
                from agents.squad.friday.agent import FridayAgent
                cls._instances[key] = FridayAgent()
            elif key == "vision":
                from agents.squad.vision.healer import VisionHealer
                cls._instances[key] = VisionHealer()
            elif key == "quill":
                from agents.squad.quill.agent import create_quill_agent
                cls._instances[key] = create_quill_agent()
            else:
                # Use generic agent for others
                cls._instances[key] = GenericAgent(
                    name=config["name"],
                    role=config["role"],
                    session_key=config["session_key"],
                    mcp_servers=config["mcp_servers"],
                    heartbeat_offset=config["heartbeat_offset"],
                    level=config.get("level", "specialist"),
                )

        return cls._instances[key]

    @classmethod
    def get_all_agents(cls) -> list[BaseAgent]:
        """Get all agent instances."""
        return [cls.get_agent(name) for name in AGENT_CONFIGS.keys()]

    @classmethod
    def list_agents(cls) -> list[dict]:
        """List all available agents with their configs."""
        return [
            {
                "name": config["name"],
                "role": config["role"],
                "session_key": config["session_key"],
                "mcp_servers": config["mcp_servers"],
            }
            for config in AGENT_CONFIGS.values()
        ]
