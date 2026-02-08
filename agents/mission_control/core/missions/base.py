"""
Base mission — defines the interface every mission workflow must implement.
"""

from abc import ABC, abstractmethod
from typing import Optional

import structlog


class BaseMission(ABC):
    """Abstract base for mission workflows.

    A mission owns the full lifecycle of a task phase:
      - BuildMission: ASSIGNED → IN_PROGRESS → REVIEW
      - VerifyMission: REVIEW → DONE | ASSIGNED
    """

    def __init__(self, agent, task_id: str, title: str, description: str,
                 mission_config: dict):
        self.agent = agent  # BaseAgent instance — provides run(), set_repo_scope(), name, logger
        self.task_id = task_id
        self.title = title
        self.description = description
        self.config = mission_config or {}
        self.logger = structlog.get_logger().bind(
            mission=self.__class__.__name__,
            task=title[:50],
            agent=agent.name,
        )

    # --- config helpers ---

    @property
    def repository(self) -> str:
        """Target repo from config, with fallback."""
        return self.config.get("repository", "Amaresh/mission-control-review")

    @property
    def source_branch(self) -> str:
        return self.config.get("source_branch", "initial-changes")

    @property
    def branch_name(self) -> str:
        return f"{self.agent.name.lower()}/{self.task_id[:8]}"

    @property
    def owner_repo(self) -> tuple[str, str]:
        parts = self.repository.split("/", 1)
        return (parts[0], parts[1]) if len(parts) == 2 else ("", "")

    @property
    def context_files(self) -> list[str]:
        return self.config.get("context_files", [])

    # --- interface ---

    @abstractmethod
    async def execute(self) -> str:
        """Run the mission workflow. Returns a summary string."""
        ...
