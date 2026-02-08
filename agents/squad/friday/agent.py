"""
Friday - Developer Agent

Handles coding tasks, bug fixes, and PR management.
Uses GenericAgent (and mission dispatch) for all task lifecycle logic.
"""

import structlog

from agents.mission_control.core.factory import GenericAgent

logger = structlog.get_logger()


class FridayAgent(GenericAgent):
    """
    Friday - The Developer.

    Responsibilities:
    - Implement features and fixes
    - Code review
    - PR management
    - Technical documentation

    Lifecycle logic is inherited from GenericAgent → BuildMission/VerifyMission.
    """

    def __init__(self):
        super().__init__(
            name="Friday",
            role="Developer",
            session_key="agent:friday:main",
            mcp_servers=["github"],
            heartbeat_offset=2,
        )

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
