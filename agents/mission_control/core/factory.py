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
                    "mission_type": in_progress.mission_type,
                    "mission_config": in_progress.mission_config,
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
                    "mission_type": task.mission_type,
                    "mission_config": task.mission_config,
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
                            {"title": t.title, "id": str(t.id), "description": t.description or "", "mission_config": t.mission_config}
                            for t in review_tasks
                        ],
                    }

        return None

    async def _do_work(self, work: dict) -> str:
        """Handle pending work."""
        from sqlalchemy import select

        from agents.mission_control.core.database import (
            AsyncSessionLocal,
            Notification,
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
            mission_config = work.get("mission_config", {})

            from agents.mission_control.core.missions import get_mission
            MissionClass = get_mission(work.get("mission_type", "build"))
            mission = MissionClass(
                agent=self,
                task_id=task_id,
                title=title,
                description=description,
                mission_config=mission_config,
            )
            return await mission.execute()

        elif work_type == "review_tasks":
            tasks = work.get("tasks", [])
            from agents.mission_control.core.missions.verify import VerifyMission
            return await VerifyMission.verify_batch(self, tasks)

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
