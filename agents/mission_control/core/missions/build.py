"""
BuildMission — handles ASSIGNED → IN_PROGRESS → REVIEW.

Extracted from GenericAgent._do_work() and FridayAgent._do_work().
Single source of truth for: branch creation, agent execution, error
recovery, PR verification, and state transitions.
"""

from datetime import datetime, timezone

import httpx
import structlog
from sqlalchemy import select

from agents.mission_control.core.missions.base import BaseMission

logger = structlog.get_logger()

DEFAULT_REPO = "Amaresh/mission-control-review"


class BuildMission(BaseMission):
    """ASSIGNED → IN_PROGRESS → REVIEW workflow."""

    async def execute(self) -> str:
        from agents.mission_control.core.database import (
            AsyncSessionLocal, Task, TaskStatus, Activity, ActivityType,
            Agent as AgentModel,
        )

        task_id = self.task_id
        title = self.title
        description = self.description
        repo_name = self.repository
        target_branch = self.source_branch
        owner, repo = self.owner_repo
        branch_name = self.branch_name

        # --- 1. Transition ASSIGNED → IN_PROGRESS ---
        async with AsyncSessionLocal() as session:
            stmt = select(Task).where(Task.id == task_id)
            result = await session.execute(stmt)
            task = result.scalar_one_or_none()
            if task and task.status == TaskStatus.ASSIGNED:
                task.status = TaskStatus.IN_PROGRESS
                agent_result = await session.execute(
                    select(AgentModel).where(AgentModel.name == self.agent.name)
                )
                agent_record = agent_result.scalar_one_or_none()
                activity = Activity(
                    type=ActivityType.TASK_STATUS_CHANGED,
                    agent_id=agent_record.id if agent_record else task_id,
                    task_id=task.id,
                    message="Status: assigned → in_progress",
                )
                session.add(activity)
                await session.commit()

        # --- 2. Create branch programmatically ---
        response = None
        success = True
        branch_exists = False

        if owner and repo:
            branch_exists = await self._ensure_branch(
                repo_name, branch_name, target_branch,
            )

        # --- 3. Execute agent ---
        try:
            repo_context = f"**Target Repository:** `{repo_name}` on GitHub\n\n"
            self.agent.set_repo_scope(repo_name)

            context_section = ""
            if self.context_files:
                context_section = (
                    f"Reference these files for context: {', '.join(self.context_files)}\n\n"
                )

            response = await self.agent.run(
                f"Execute the following task by calling GitHub MCP tools NOW. "
                f"Do not explain, plan, or ask questions — just call the tools.\n\n"
                f"Task: {title}\n"
                f"Description: {description}\n\n"
                f"{repo_context}"
                f"{context_section}"
                f"Branch `{branch_name}` already exists on `{target_branch}`. "
                f"Do NOT call create_branch. Go directly to step 2.\n\n"
                f"STEP 2: Call `create_or_update_file` to create deliverable files. "
                f"Use owner=\"{owner}\", repo=\"{repo}\", "
                f"branch=\"{branch_name}\". Write real, complete implementation code — "
                f"NOT plan documents, NOT outlines, NOT markdown breakdowns. "
                f"Deliver .py/.js/.ts/.yaml files with working logic. "
                f"The path is relative to the repo root (e.g. \"src/monitoring/health.py\").\n\n"
                f"STEP 3: Call `create_pull_request` with owner=\"{owner}\", "
                f"repo=\"{repo}\", head=\"{branch_name}\", base=\"{target_branch}\", "
                f"title=\"{title}\".\n\n"
                f"RULES: No local git/shell/filesystem commands. No Copilot skills. "
                f"No update_task_status. No plan/outline/breakdown .md files. Only repo `{repo_name}`. "
                f"No create_branch calls. Call the first tool now."
            )
        except Exception as e:
            self.logger.error("Agent run failed", error=str(e))
            response = f"ERROR: {e}"
            success = False
        finally:
            self.agent.set_repo_scope(None)

            # --- 4. Capture learning ---
            from agents.mission_control.learning.capture import capture_task_outcome
            await capture_task_outcome(
                agent_name=self.agent.name,
                task_id=str(task_id),
                task_title=title,
                from_status="in_progress",
                to_status="review",
                duration_seconds=0.0,
                success=success,
                response_preview=response[:200] if response else None,
                error=response if not success else None,
            )

            # --- 5. Error recovery → reset to ASSIGNED ---
            if not success or (response and any(
                err in response for err in ["Broken pipe", "Cannot proceed", "ERROR:"]
            )):
                self.logger.warning(
                    "Agent errored — resetting to ASSIGNED for retry",
                    response=response[:100] if response else None,
                )
                async with AsyncSessionLocal() as session:
                    stmt = select(Task).where(Task.id == task_id)
                    result = await session.execute(stmt)
                    task = result.scalar_one_or_none()
                    if task and task.status == TaskStatus.IN_PROGRESS:
                        task.status = TaskStatus.ASSIGNED
                        session.add(Activity(
                            type=ActivityType.TASK_STATUS_CHANGED,
                            task_id=task.id,
                            message="Status: in_progress → assigned (agent error, will retry)",
                        ))
                        await session.commit()
                return f"Error (will retry): {title}"

            # --- 6. PR check → transition IN_PROGRESS → REVIEW ---
            from agents.mission_control.core.pr_check import has_open_pr
            pr_found = False
            head_prefix = f"{self.agent.name.lower()}/"
            pr_found, pr_url = await has_open_pr(repo_name, head_prefix)
            if pr_found:
                self.logger.info("PR verified", pr=pr_url, repo=repo_name)
            else:
                # Fallback: create PR programmatically
                pr_found, pr_url = await self.agent._create_pr_fallback(
                    repo_name, branch_name, target_branch, title,
                )
                if not pr_found:
                    self.logger.warning(
                        "Task kept IN_PROGRESS — no open PR found",
                        repo=repo_name, head_prefix=head_prefix,
                    )

            if pr_found:
                async with AsyncSessionLocal() as session:
                    stmt = select(Task).where(Task.id == task_id)
                    result = await session.execute(stmt)
                    task = result.scalar_one_or_none()
                    if task and task.status == TaskStatus.IN_PROGRESS:
                        task.status = TaskStatus.REVIEW
                        agent_result = await session.execute(
                            select(AgentModel).where(AgentModel.name == self.agent.name)
                        )
                        agent_record = agent_result.scalar_one_or_none()
                        msg = "Status: in_progress → review"
                        if response:
                            msg += f". Agent output: {response[:200]}"
                        session.add(Activity(
                            type=ActivityType.TASK_STATUS_CHANGED,
                            agent_id=agent_record.id if agent_record else task_id,
                            task_id=task.id,
                            message=msg,
                        ))
                        await session.commit()
                        self.logger.info("Task moved to review")

        return f"Completed: {title}"

    # --- helpers ---

    async def _ensure_branch(
        self, repo_name: str, branch_name: str, base_branch: str,
    ) -> bool:
        """Create branch via GitHub API if it doesn't exist. Returns True if branch exists."""
        from agents.config import settings as _settings
        token = _settings.github_token
        if not token:
            return False
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo_name}/branches/{branch_name}",
                    headers=headers,
                )
                if resp.status_code == 200:
                    self.logger.info("Branch already exists", branch=branch_name)
                    return True
                # Get base SHA and create
                base_resp = await client.get(
                    f"https://api.github.com/repos/{repo_name}/git/ref/heads/{base_branch}",
                    headers=headers,
                )
                if base_resp.status_code == 200:
                    base_sha = base_resp.json()["object"]["sha"]
                    create_resp = await client.post(
                        f"https://api.github.com/repos/{repo_name}/git/refs",
                        headers=headers,
                        json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
                    )
                    if create_resp.status_code == 201:
                        self.logger.info("Created branch", branch=branch_name, base=base_branch)
                        return True
                    else:
                        self.logger.warning("Failed to create branch", status=create_resp.status_code)
        except Exception as e:
            self.logger.warning("Branch creation failed", error=str(e))
        return False
