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

    # --- transition validation ---

    def validate_transition(self, from_state: str, to_state: str) -> bool:
        """Check if a transition is valid per workflows.yaml.
        
        If blocked, records the guard block for monitoring.
        """
        from mission_control.mission_control.core.workflow_loader import get_workflow_loader
        result = get_workflow_loader().validate_transition(
            self._mission_type, from_state, to_state,
        )
        if not result:
            # Fire-and-forget: record the block for guard monitor
            import asyncio
            try:
                from mission_control.mission_control.learning.guard_monitor import record_guard_block
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(record_guard_block(
                        mission_type=self._mission_type,
                        from_state=from_state,
                        to_state=to_state,
                        guard_name="workflow_transition",
                        agent_name=self.agent.name,
                        task_id=str(self.task_id),
                    ))
            except Exception:
                pass
        return result

    @property
    def _mission_type(self) -> str:
        """Override in subclasses."""
        return "build"

    # --- pattern injection ---

    async def get_learned_context(self) -> str:
        """Retrieve relevant patterns for this mission and format for LLM injection."""
        try:
            from mission_control.mission_control.learning.capture import (
                get_relevant_patterns,
                format_patterns_for_context,
            )
            patterns = await get_relevant_patterns(
                query=f"{self.title} {self.description or ''}",
                mission_type=self._mission_type,
                limit=5,
            )
            return format_patterns_for_context(patterns)
        except Exception:
            return ""

    # --- transition event capture ---

    async def capture_transition(
        self, from_state: str, to_state: str,
        duration_sec: float = 0.0,
        guard: Optional[str] = None, guard_result: Optional[bool] = None,
    ):
        """Fire-and-forget capture of a state transition."""
        try:
            from mission_control.mission_control.learning.capture import capture_mission_transition
            await capture_mission_transition(
                agent_name=self.agent.name,
                mission_type=self._mission_type,
                task_id=str(self.task_id),
                from_state=from_state,
                to_state=to_state,
                duration_in_prev_state_sec=duration_sec,
                guard_evaluated=guard,
                guard_result=guard_result,
            )
        except Exception:
            pass  # fire-and-forget
