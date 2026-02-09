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

from mission_control.mission_control.core.missions.base import BaseMission

logger = structlog.get_logger()

DEFAULT_REPO = "{owner}/{target-repo}"


class BuildMission(BaseMission):
    """ASSIGNED → IN_PROGRESS → REVIEW workflow."""

    async def execute(self) -> str:
        from mission_control.mission_control.core.database import (
            AsyncSessionLocal, Task, TaskStatus, Activity, ActivityType,
            Agent as AgentModel,
        )
        import time as _time

        task_id = self.task_id
        title = self.title
        description = self.description
        repo_name = self.repository
        target_branch = self.source_branch
        owner, repo = self.owner_repo
        branch_name = self.branch_name
        t0 = _time.monotonic()

        # --- 1. Transition ASSIGNED → IN_PROGRESS ---
        async with AsyncSessionLocal() as session:
            stmt = select(Task).where(Task.id == task_id)
            result = await session.execute(stmt)
            task = result.scalar_one_or_none()
            if task and task.status == TaskStatus.ASSIGNED:
                if not self.validate_transition("ASSIGNED", "IN_PROGRESS"):
                    self.logger.error("Transition ASSIGNED→IN_PROGRESS not allowed")
                    return f"Blocked: invalid transition for {title}"
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
                await self.capture_transition("ASSIGNED", "IN_PROGRESS")

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

            # Inject learned patterns from previous missions
            learned = await self.get_learned_context()
            learned_section = f"{learned}\n\n" if learned else ""

            response = await self.agent.run(
                f"Execute the following task by calling GitHub MCP tools NOW. "
                f"Do not explain, plan, or ask questions — just call the tools.\n\n"
                f"Task: {title}\n"
                f"Description: {description}\n\n"
                f"{repo_context}"
                f"{context_section}"
                f"{learned_section}"
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

            # --- 4. PR check — the ONLY thing that matters ---
            # LLM response is just metadata. The ground truth is: does a PR exist?
            from mission_control.mission_control.core.pr_check import has_open_pr
            pr_found = False
            pr_url = None
            head_prefix = f"{self.agent.name.lower()}/"

            if success:
                pr_found, pr_url = await has_open_pr(repo_name, head_prefix)
                if not pr_found:
                    # Fallback: create PR programmatically
                    pr_found, pr_url = await self.agent._create_pr_fallback(
                        repo_name, branch_name, target_branch, title,
                    )

            # --- 5. Capture learning (with actual outcome) ---
            from mission_control.mission_control.learning.capture import capture_task_outcome
            await capture_task_outcome(
                agent_name=self.agent.name,
                task_id=str(task_id),
                task_title=title,
                from_status="in_progress",
                to_status="review" if pr_found else "assigned",
                duration_seconds=_time.monotonic() - t0,
                success=pr_found,
                response_preview=response[:200] if response else None,
                error=response[:500] if (not pr_found and response) else None,
                mission_type=self._mission_type,
            )

            # --- 6. Transition based on PR existence ---
            if pr_found:
                self.logger.info("PR verified", pr=pr_url, repo=repo_name)
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
                        if pr_url:
                            msg += f" (PR: {pr_url})"
                        session.add(Activity(
                            type=ActivityType.TASK_STATUS_CHANGED,
                            agent_id=agent_record.id if agent_record else task_id,
                            task_id=task.id,
                            message=msg,
                        ))
                        await session.commit()
                        self.logger.info("Task moved to review")
                await self.capture_transition(
                    "IN_PROGRESS", "REVIEW",
                    duration_sec=_time.monotonic() - t0,
                    guard="has_open_pr", guard_result=True,
                )
                from mission_control.mission_control.learning.capture import capture_mission_complete
                await capture_mission_complete(
                    agent_name=self.agent.name,
                    mission_type=self._mission_type,
                    task_id=str(task_id),
                    total_duration_sec=_time.monotonic() - t0,
                    transition_path="ASSIGNED→IN_PROGRESS→REVIEW",
                )
            else:
                # No PR = not done. Reset to ASSIGNED. Store LLM response for Vision.
                reason = response[:300] if response else "Agent produced no output"
                self.logger.warning("No PR found — resetting to ASSIGNED", reason=reason[:100])
                async with AsyncSessionLocal() as session:
                    stmt = select(Task).where(Task.id == task_id)
                    result = await session.execute(stmt)
                    task = result.scalar_one_or_none()
                    if task and task.status == TaskStatus.IN_PROGRESS:
                        task.status = TaskStatus.ASSIGNED
                        session.add(Activity(
                            type=ActivityType.TASK_STATUS_CHANGED,
                            task_id=task.id,
                            message=f"Status: in_progress → assigned (no PR). Agent said: {reason}",
                        ))
                        await session.commit()
                await self.capture_transition("IN_PROGRESS", "ASSIGNED", guard="has_open_pr", guard_result=False)
                from mission_control.mission_control.learning.capture import capture_error_recovery
                await capture_error_recovery(
                    agent_name=self.agent.name,
                    mission_type=self._mission_type,
                    task_id=str(task_id),
                    error_message=reason,
                )
                return f"No PR — reset to ASSIGNED: {title}"

        return f"Completed: {title}"

    # --- helpers ---

    async def _ensure_branch(
        self, repo_name: str, branch_name: str, base_branch: str,
    ) -> bool:
        """Create branch via GitHub API if it doesn't exist. Returns True if branch exists."""
        from mission_control.config import settings as _settings
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
