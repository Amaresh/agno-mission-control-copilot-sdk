"""
Agent factory - creates and manages agent instances.
"""

from datetime import datetime, timezone
from typing import Optional

import structlog

from mission_control.mission_control.core.base_agent import BaseAgent
from mission_control.mission_control.core.workflow_loader import get_workflow_loader

logger = structlog.get_logger()


def _get_agent_configs() -> dict:
    """Get agent configs from workflow loader (reads workflows.yaml)."""
    return get_workflow_loader().get_agent_configs_as_legacy()


class GenericAgent(BaseAgent):
    """Generic agent implementation for squad members."""

    async def _send_telegram_notification(self, content: str):
        """Send a notification directly to Telegram instead of relying on the LLM."""
        from mission_control.config import settings
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

        from mission_control.mission_control.core.database import (
            Agent as AgentModel,
        )
        from mission_control.mission_control.core.database import (
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
                Notification.delivered == False,
            ).limit(3)

            result = await session.execute(stmt)
            notifications = result.scalars().all()

            if notifications:
                return {
                    "type": "notifications",
                    "items": [{"id": str(n.id), "content": n.content} for n in notifications]
                }

            # Resume IN_PROGRESS task if one exists (prevents deadlock)
            # Also check custom pipeline states from config
            pipeline_states = [TaskStatus.IN_PROGRESS]
            custom_states = get_workflow_loader().get_all_mission_states()
            builtin = {"ASSIGNED", "IN_PROGRESS", "DONE"}
            for s in custom_states:
                if s not in builtin and hasattr(TaskStatus, s):
                    pipeline_states.append(TaskStatus(s))
            stmt = (
                select(Task)
                .join(TaskAssignment, TaskAssignment.task_id == Task.id)
                .where(
                    TaskAssignment.agent_id == agent.id,
                    Task.status.in_(pipeline_states),
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
                    "mission_type": in_progress.mission_type,
                    "mission_config": in_progress.mission_config,
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
                            {
                                "title": t.title,
                                "task_id": str(t.id),
                                "id": str(t.id),
                                "description": t.description or "",
                                "mission_config": t.mission_config,
                            }
                            for t in review_tasks
                        ],
                    }

        return None

    async def _do_work(self, work: dict) -> str:
        """Handle pending work."""
        from sqlalchemy import select

        from mission_control.mission_control.core.database import (
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

            from mission_control.mission_control.core.missions import get_mission
            from mission_control.mission_control.core.missions.generic import GenericMission
            MissionClass = get_mission(work.get("mission_type", "build"))
            kwargs = dict(
                agent=self,
                task_id=task_id,
                title=title,
                description=description,
                mission_config=mission_config,
            )
            if MissionClass is GenericMission:
                kwargs["mission_type"] = work.get("mission_type", "build")
            mission = MissionClass(**kwargs)
            return await mission.execute()

        elif work_type == "review_tasks":
            tasks = work.get("tasks", [])
            from mission_control.mission_control.core.missions.verify import VerifyMission
            return await VerifyMission.verify_batch(self, tasks)

        return "HEARTBEAT_OK"


class AgentFactory:
    """Factory for creating agent instances."""

    _instances: dict[str, BaseAgent] = {}
    _synced: bool = False

    @classmethod
    async def sync_agent_configs(cls):
        """Sync AGENT_CONFIGS → DB so roles/levels never drift.

        Called once at startup. Creates missing agents and updates
        stale role/level values to match the authoritative config.
        """
        if cls._synced:
            return

        from sqlalchemy import select

        from mission_control.mission_control.core.database import (
            Agent as AgentModel,
        )
        from mission_control.mission_control.core.database import (
            AgentLevel,
            AsyncSessionLocal,
        )

        try:
            async with AsyncSessionLocal() as session:
                for key, config in _get_agent_configs().items():
                    name = config["name"]
                    role = config["role"]
                    level = AgentLevel(config.get("level", "specialist"))

                    stmt = select(AgentModel).where(AgentModel.name == name)
                    result = await session.execute(stmt)
                    agent = result.scalar_one_or_none()

                    if agent:
                        changed = []
                        if agent.role != role:
                            changed.append(f"role: {agent.role!r} → {role!r}")
                            agent.role = role
                        if agent.level != level:
                            changed.append(f"level: {agent.level} → {level}")
                            agent.level = level
                        if changed:
                            logger.info("Synced agent config", agent=name, changes=changed)
                    else:
                        agent = AgentModel(name=name, role=role, level=level)
                        session.add(agent)
                        logger.info("Created agent record", agent=name)

                await session.commit()
            cls._synced = True
            logger.info("Agent config sync complete")
        except Exception as e:
            logger.error("Agent config sync failed", error=str(e))

    @classmethod
    def get_agent(cls, name: str) -> BaseAgent:
        """Get or create an agent instance."""
        key = name.lower()

        if key not in cls._instances:
            configs = _get_agent_configs()
            if key not in configs:
                raise ValueError(f"Unknown agent: {name}")

            config = configs[key]

            # Use specialized agents for Jarvis and Vision
            if key == "jarvis":
                from mission_control.squad.jarvis.agent import JarvisAgent
                cls._instances[key] = JarvisAgent()
            elif key == "vision":
                from mission_control.squad.vision.healer import VisionHealer
                cls._instances[key] = VisionHealer()
            else:
                # All other agents (including Friday, Quill) use GenericAgent
                cls._instances[key] = GenericAgent(
                    name=config["name"],
                    role=config["role"],
                    session_key=config["session_key"],
                    mcp_servers=config["mcp_servers"],
                    heartbeat_offset=config["heartbeat_offset"],
                    level=config.get("level", "specialist"),
                    always_run=config.get("always_run"),
                )

        return cls._instances[key]

    @classmethod
    def get_all_agents(cls) -> list[BaseAgent]:
        """Get all agent instances."""
        return [cls.get_agent(name) for name in _get_agent_configs().keys()]

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
            for config in _get_agent_configs().values()
        ]
