"""
GenericMission — universal config-driven mission executor.

Replaces BuildMission and ContentMission with a single class that reads
its behaviour from workflows.yaml:

  stages:
    RESEARCH:
      pre_actions: [{action: tavily_search, query: "..."}]
      prompt_template: content_research
      post_actions: [{action: github_commit, path: "..."}]

The 5-step execute loop:
  1. Determine current stage from task status
  2. Run pre_actions (gather context via ActionRunner)
  3. Build prompt from template + context
  4. Run agent (LLM generates response, may call MCP tools)
  5. Run post_actions (commit deliverables) + transition & reassign
"""

import time as _time

import structlog
from sqlalchemy import delete, select

from mission_control.mission_control.core.actions import ActionRunner
from mission_control.mission_control.core.missions.base import BaseMission
from mission_control.mission_control.core.prompt_loader import PromptLoader

logger = structlog.get_logger()

_prompt_loader = PromptLoader()


class GenericMission(BaseMission):
    """Config-driven mission — all behaviour defined in workflows.yaml."""

    def __init__(self, agent, task_id, title, description, mission_config,
                 mission_type: str = "build"):
        super().__init__(agent, task_id, title, description, mission_config)
        self._type = mission_type
        # Merge default_config from workflows.yaml (task config overrides defaults)
        mdef = self._get_mission_def()
        defaults = mdef.get("default_config", {})
        if defaults:
            merged = {**defaults, **self.config}
            self.config = merged

    @property
    def _mission_type(self) -> str:
        return self._type

    # ------------------------------------------------------------------
    # Config accessors (read from workflows.yaml via WorkflowLoader)
    # ------------------------------------------------------------------

    def _get_mission_def(self) -> dict:
        from mission_control.mission_control.core.workflow_loader import get_workflow_loader
        return get_workflow_loader().get_mission_def(self._type)

    def _get_stage_config(self, stage: str) -> dict:
        """Get the stage config from workflows.yaml missions.<type>.stages.<STAGE>."""
        return self._get_mission_def().get("stages", {}).get(stage, {})

    def _get_state_agents(self) -> dict[str, str]:
        """Map of STATE → agent role name for hand-offs."""
        return self._get_mission_def().get("state_agents", {})

    def _get_verify_strategy(self) -> str:
        return self._get_mission_def().get("verify_strategy", "pr")

    def _get_next_state(self, current: str) -> str | None:
        """Find the default (non-error) next state from transitions."""
        mdef = self._get_mission_def()
        for t in mdef.get("transitions", []):
            if t["from"] == current and t.get("guard") != "has_error":
                return t["to"]
        return None

    async def _evaluate_transition_guard(
        self, current_state: str, next_state: str, task_vars: dict,
    ) -> tuple[bool, str | None]:
        """Evaluate the guard on the transition from current→next.

        Returns (passed, guard_name). If no guard is defined, returns (True, None).
        """
        from mission_control.mission_control.core.guards import GuardRegistry

        mdef = self._get_mission_def()
        for t in mdef.get("transitions", []):
            if t["from"] == current_state and t["to"] == next_state:
                guard_name = t.get("guard")
                if not guard_name or guard_name == "has_error":
                    return True, None
                guard_fn = GuardRegistry.get(guard_name)
                if not guard_fn:
                    self.logger.warning("Guard not found", guard=guard_name)
                    return True, guard_name
                try:
                    result = await guard_fn(task_vars)
                    return result, guard_name
                except Exception as e:
                    self.logger.error("Guard failed", guard=guard_name, error=str(e))
                    return False, guard_name
        return True, None

    def _get_initial_state(self) -> str:
        return self._get_mission_def().get("initial_state", "ASSIGNED")

    # ------------------------------------------------------------------
    # Execute: the universal 5-step loop
    # ------------------------------------------------------------------

    async def execute(self) -> str:
        from mission_control.mission_control.core.database import (
            Activity,
            ActivityType,
            AsyncSessionLocal,
            Task,
            TaskAssignment,
            TaskStatus,
        )
        from mission_control.mission_control.core.database import (
            Agent as AgentModel,
        )

        task_id = self.task_id
        title = self.title
        description = self.description
        t0 = _time.monotonic()

        # --- 1. Determine current stage ---
        async with AsyncSessionLocal() as session:
            task = (await session.execute(
                select(Task).where(Task.id == task_id)
            )).scalar_one_or_none()
            if not task:
                return f"Task {task_id} not found"

            current_state = task.status.name
            initial = self._get_initial_state()

            # Auto-transition from ASSIGNED to initial state if needed
            if current_state == "ASSIGNED" and initial != "ASSIGNED":
                if hasattr(TaskStatus, initial):
                    task.status = TaskStatus[initial]
                    await session.commit()
                    current_state = initial

        next_state = self._get_next_state(current_state)
        if not next_state:
            return f"Task at terminal state: {current_state}"

        stage_cfg = self._get_stage_config(current_state)

        # Build template variables
        owner, repo = self.owner_repo
        short_id = str(task_id)[:8]
        task_vars = {
            "task_id": str(task_id),
            "short_id": short_id,
            "title": title,
            "description": description or "",
            "repository": self.repository,
            "owner": owner,
            "repo": repo,
            "branch_name": self.branch_name,
            "source_branch": self.source_branch,
            "mission_type": self._type,
            "current_state": current_state,
            "next_state": next_state,
        }

        runner = ActionRunner(task_vars)

        # --- 2. Pre-actions: gather context ---
        pre_results = {}
        for action_cfg in stage_cfg.get("pre_actions", []):
            name = action_cfg.get("action", "unknown")
            pre_results[name] = await runner.run(action_cfg)

        # Merge pre-action results into context_data (for prompt templates)
        context_data = "\n\n".join(
            str(v) for v in pre_results.values() if v
        ) or description or ""
        task_vars["context_data"] = context_data
        task_vars["context_files_section"] = (
            f"Reference these files for context: {', '.join(self.context_files)}"
            if self.context_files else ""
        )

        # --- 3. Build prompt ---
        learned = await self.get_learned_context()
        task_vars["learned_context"] = learned or ""

        prompt_name = stage_cfg.get("prompt_template", "")
        if prompt_name:
            # Composite: base + stage template (for content missions)
            base_template = stage_cfg.get("prompt_base", "")
            if base_template:
                prompt = _prompt_loader.render_composite(
                    [base_template, prompt_name], **task_vars
                )
            else:
                prompt = _prompt_loader.render(prompt_name, **task_vars)
        else:
            # Fallback: inline prompt for missions without templates
            prompt = f"Task: {title}\nDescription: {description}"

        if not prompt.strip():
            return f"Empty prompt for stage {current_state}"

        # --- 4. Run agent ---
        response = None
        success = True
        try:
            self.agent.set_repo_scope(self.repository)
            response = await self.agent.run(prompt)
        except Exception as e:
            self.logger.error("Agent run failed", error=str(e), stage=current_state)
            response = f"ERROR: {e}"
            success = False
        finally:
            self.agent.set_repo_scope(None)

        # --- 5. Post-actions + transition ---
        # Run post-condition check if defined (e.g. pr_check for build)
        post_check = stage_cfg.get("post_check")
        deliverable_ok = success

        if post_check == "pr_exists":
            from mission_control.mission_control.core.pr_check import has_open_pr
            head_prefix = f"{self.agent.name.lower()}/"
            pr_found, pr_url = await has_open_pr(self.repository, head_prefix)
            if not pr_found:
                # Fallback: try to create PR programmatically
                pr_found, pr_url = await self.agent._create_pr_fallback(
                    self.repository, self.branch_name,
                    self.source_branch, title,
                )
            deliverable_ok = pr_found
            if pr_found:
                self.logger.info("PR verified", pr=pr_url)
        elif post_check == "review_approved":
            deliverable_ok = response and "[APPROVED]" in response[:200]
            if response and "[REVISION]" in response[:200]:
                # Revision loop: go to DRAFT instead of PUBLISH
                next_state = "DRAFT"
                deliverable_ok = True

        # Run post_actions (e.g. github_commit)
        if deliverable_ok and stage_cfg.get("post_actions"):
            task_vars["llm_output"] = response or ""
            # Clean llm_output for review stage
            if post_check == "review_approved" and response:
                task_vars["llm_output"] = response.replace("[APPROVED]", "").strip()
            for action_cfg in stage_cfg.get("post_actions", []):
                await runner.run(action_cfg, extra_vars={"llm_output": task_vars["llm_output"]})

        # --- Evaluate transition guard ---
        if deliverable_ok and next_state:
            guard_passed, guard_name = await self._evaluate_transition_guard(
                current_state, next_state, task_vars,
            )
            if not guard_passed:
                self.logger.warning(
                    "Guard blocked transition",
                    guard=guard_name, from_state=current_state, to_state=next_state,
                )
                deliverable_ok = False

        # --- Transition or reset ---
        async with AsyncSessionLocal() as session:
            task = (await session.execute(
                select(Task).where(Task.id == task_id)
            )).scalar_one_or_none()
            if not task:
                return f"Task disappeared: {task_id}"

            agent_result = await session.execute(
                select(AgentModel).where(AgentModel.name == self.agent.name)
            )
            agent_record = agent_result.scalar_one_or_none()

            if deliverable_ok and next_state:
                old_status = task.status
                if hasattr(TaskStatus, next_state):
                    task.status = TaskStatus[next_state]
                session.add(Activity(
                    type=ActivityType.TASK_STATUS_CHANGED,
                    agent_id=agent_record.id if agent_record else task_id,
                    task_id=task.id,
                    message=f"{self._type}: {old_status.name} → {next_state}",
                ))

                # Reassign to next agent if state_agents defines one
                await self._reassign_to_next_agent(
                    session, task, next_state, AgentModel, TaskAssignment,
                    from_state=old_status.name,
                )

                await session.commit()
                await self.capture_transition(
                    current_state, next_state,
                    duration_sec=_time.monotonic() - t0,
                )
                self.logger.info(
                    "Stage complete",
                    stage=current_state, next=next_state, task=title[:50],
                )

                if next_state == "DONE":
                    from mission_control.mission_control.learning.capture import (
                        capture_mission_complete,
                    )
                    await capture_mission_complete(
                        agent_name=self.agent.name,
                        mission_type=self._type,
                        task_id=str(task_id),
                        total_duration_sec=_time.monotonic() - t0,
                        transition_path=f"…→{current_state}→DONE",
                    )

                return f"{self._type} {current_state}→{next_state}: {title}"
            else:
                # Failed — reset or keep state
                error_state = self._get_error_state(current_state)
                reason = response[:300] if response else "No output"
                self.logger.warning(
                    "Deliverable check failed — resetting",
                    stage=current_state, error_state=error_state,
                )
                if error_state and hasattr(TaskStatus, error_state):
                    task.status = TaskStatus[error_state]
                session.add(Activity(
                    type=ActivityType.TASK_STATUS_CHANGED,
                    agent_id=agent_record.id if agent_record else task_id,
                    task_id=task.id,
                    message=f"{self._type} {current_state}: failed. {reason[:200]}",
                ))
                await session.commit()
                await self.capture_transition(
                    current_state, error_state or current_state,
                    guard="post_check", guard_result=False,
                )
                # Capture error for learning
                from mission_control.mission_control.learning.capture import capture_error_recovery
                await capture_error_recovery(
                    agent_name=self.agent.name,
                    mission_type=self._type,
                    task_id=str(task_id),
                    error_message=reason,
                )
                return f"{self._type} {current_state} failed: {title}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_error_state(self, current_state: str) -> str | None:
        """Find the error/fallback state from transitions (guard: has_error)."""
        mdef = self._get_mission_def()
        for t in mdef.get("transitions", []):
            if t["from"] == current_state and t.get("guard") == "has_error":
                return t["to"]
        return None

    async def _reassign_to_next_agent(
        self, session, task, next_state: str,
        AgentModel, TaskAssignment,  # noqa: N803
        from_state: str | None = None,
    ):
        """Hand off task to the agent responsible for next_state."""
        state_agents = self._get_state_agents()
        next_role = state_agents.get(next_state)
        if not next_role or next_state == "DONE":
            return

        # Skip if same role handles both states (build pattern — sticky)
        current_role = state_agents.get(from_state) if from_state else None
        if next_role == current_role:
            return

        next_agent = (await session.execute(
            select(AgentModel).where(AgentModel.role == next_role)
        )).scalar_one_or_none()
        if not next_agent:
            self.logger.warning("Next agent not found", role=next_role)
            return

        await session.execute(
            delete(TaskAssignment).where(TaskAssignment.task_id == task.id)
        )
        session.add(TaskAssignment(task_id=task.id, agent_id=next_agent.id))
        self.logger.info(
            "Reassigned to next agent",
            next_agent=next_agent.name, next_role=next_role,
        )
