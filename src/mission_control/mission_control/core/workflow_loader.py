"""
WorkflowLoader — loads workflows.yaml into python-statemachine models.

Singleton: loads once at import, exposes get_mission(), get_agent_config(),
get_all_agent_configs(). Supports hot-reload via reload().
"""

from pathlib import Path
from typing import Optional

import structlog
import yaml
from statemachine import State, StateMachine

from mission_control.mission_control.core.guards import GuardRegistry
from mission_control.mission_control.core.actions import _ACTION_HANDLERS

logger = structlog.get_logger()

# Known post_check values recognized by GenericMission.execute()
_VALID_POST_CHECKS = {"pr_exists", "review_approved"}

# Default path — use centralized path resolver
from mission_control.paths import workflows_yaml as _workflows_yaml_path

_DEFAULT_YAML = _workflows_yaml_path()


def _build_state_machine(name: str, mission_def: dict) -> type[StateMachine]:
    """Build a StateMachine subclass from a mission YAML definition.

    Returns a class (not instance) that can validate transitions.
    """
    transitions = mission_def.get("transitions", [])
    initial_state_name = mission_def.get("initial_state", "ASSIGNED")

    # Collect unique state names
    state_names = set()
    for t in transitions:
        state_names.add(t["from"])
        state_names.add(t["to"])

    # Build State objects
    states = {}
    # Identify which states have outgoing transitions
    sources = {t["from"] for t in transitions}
    for sn in state_names:
        is_final = sn not in sources
        states[sn] = State(sn, initial=(sn == initial_state_name), final=is_final)

    # Build transition specs
    transition_specs = []
    for t in transitions:
        event_name = f"{t['from'].lower()}_to_{t['to'].lower()}"
        guard_name = t.get("guard")
        transition_specs.append({
            "from": t["from"],
            "to": t["to"],
            "guard": guard_name,
            "event": event_name,
        })

    # Create class dynamically
    attrs = {}
    attrs["_states"] = states
    attrs["_transitions"] = transition_specs
    attrs["_mission_name"] = name

    # Add state attributes
    for sn, state_obj in states.items():
        attrs[f"state_{sn}"] = state_obj

    # Add transitions as events
    for spec in transition_specs:
        from_s = states[spec["from"]]
        to_s = states[spec["to"]]
        event = from_s.to(to_s)
        attrs[spec["event"]] = event

    cls = type(f"{name.title()}StateMachine", (StateMachine,), attrs)
    return cls


class WorkflowLoader:
    """Singleton loader for workflows.yaml."""

    _instance: Optional["WorkflowLoader"] = None
    _missions: dict[str, dict] = {}
    _agents: dict[str, dict] = {}
    _state_machines: dict[str, type[StateMachine]] = {}
    _yaml_path: Path = _DEFAULT_YAML
    _loaded: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load(self, yaml_path: Path | str | None = None):
        """Load or reload from YAML file."""
        if yaml_path:
            self._yaml_path = Path(yaml_path)

        if not self._yaml_path.exists():
            logger.warning(
                "workflows.yaml not found, using hardcoded defaults",
                path=str(self._yaml_path),
            )
            self._load_defaults()
            return

        with open(self._yaml_path) as f:
            data = yaml.safe_load(f)

        # Validate before loading — block hard errors, log warnings
        issues = self.validate_yaml(data)
        hard_errors = [e for e in issues if not e.startswith("[warning]")]
        soft_warnings = [e.removeprefix("[warning] ") for e in issues if e.startswith("[warning]")]
        for w in soft_warnings:
            logger.warning("Workflow validation", issue=w)
        if hard_errors:
            for e in hard_errors:
                logger.error("Workflow validation error", error=e)
            raise ValueError(
                f"workflows.yaml has {len(hard_errors)} error(s): {hard_errors[0]}"
            )

        self._missions = data.get("missions", {})
        self._agents = data.get("agents", {})

        # Build state machines for each mission
        self._state_machines = {}
        for mname, mdef in self._missions.items():
            try:
                self._state_machines[mname] = _build_state_machine(mname, mdef)
                logger.info("Built state machine", mission=mname)
            except Exception as e:
                logger.error("Failed to build state machine", mission=mname, error=str(e))

        self._loaded = True
        logger.info(
            "Workflow config loaded",
            missions=list(self._missions.keys()),
            agents=list(self._agents.keys()),
            path=str(self._yaml_path),
        )

    def _load_defaults(self):
        """Fallback: use hardcoded defaults matching the old AGENT_CONFIGS/MISSION_REGISTRY."""
        self._missions = {
            "build": {
                "description": "Developer workflow: branch → code → PR",
                "initial_state": "ASSIGNED",
                "transitions": [
                    {"from": "ASSIGNED", "to": "IN_PROGRESS"},
                    {"from": "IN_PROGRESS", "to": "REVIEW", "guard": "has_open_pr"},
                    {"from": "IN_PROGRESS", "to": "ASSIGNED", "guard": "has_error"},
                ],
                "default_config": {"source_branch": "main"},
            },
            "verify": {
                "description": "Review workflow: check PR exists → approve or reject",
                "initial_state": "REVIEW",
                "transitions": [
                    {"from": "REVIEW", "to": "DONE", "guard": "has_open_pr"},
                    {"from": "REVIEW", "to": "ASSIGNED", "guard": "no_open_pr"},
                ],
            },
        }
        self._agents = {
            "jarvis": {"name": "Jarvis", "role": "Squad Lead / Coordinator", "level": "lead",
                       "mission": "verify", "mcp_servers": ["github", "telegram"], "heartbeat_offset": 0},
            "friday": {"name": "Friday", "role": "Developer", "level": "specialist",
                       "mission": "build", "mcp_servers": ["github"], "heartbeat_offset": 2},
            "vision": {"name": "Vision", "role": "System Healer / Ops Monitor", "level": "lead",
                       "mission": None, "agent_class": "healer", "mcp_servers": [], "heartbeat_offset": 0},
            "wong": {"name": "Wong", "role": "Documentation", "level": "specialist",
                     "mission": "build", "mcp_servers": ["github"], "heartbeat_offset": 6},
            "shuri": {"name": "Shuri", "role": "Testing & QA", "level": "specialist",
                      "mission": "build", "mcp_servers": ["github"], "heartbeat_offset": 8},
            "fury": {"name": "Fury", "role": "Developer", "level": "specialist",
                     "mission": "build", "mcp_servers": ["github"], "heartbeat_offset": 10},
            "pepper": {"name": "Pepper", "role": "Developer", "level": "specialist",
                       "mission": "build", "mcp_servers": ["github"], "heartbeat_offset": 12}
        }
        self._loaded = True
        logger.info("Using hardcoded workflow defaults (no workflows.yaml found)")

    def ensure_loaded(self):
        """Lazy-load on first access."""
        if not self._loaded:
            self.load()

    # ── Public API ───────────────────────────────────────────────

    def get_mission_def(self, mission_type: str) -> dict:
        """Get the raw mission definition dict."""
        self.ensure_loaded()
        return self._missions.get(mission_type, self._missions.get("build", {}))

    def get_mission_class(self, mission_type: str):
        """Get the mission execution class — GenericMission for all non-verify."""
        self.ensure_loaded()
        from mission_control.mission_control.core.missions.verify import VerifyMission
        if mission_type == "verify":
            return VerifyMission
        from mission_control.mission_control.core.missions.generic import GenericMission
        return GenericMission

    def get_all_mission_states(self) -> set[str]:
        """Collect all states declared across all missions (explicit + from transitions)."""
        self.ensure_loaded()
        states: set[str] = set()
        for mdef in self._missions.values():
            for s in mdef.get("states", []):
                states.add(s)
            for t in mdef.get("transitions", []):
                states.add(t["from"])
                states.add(t["to"])
        return states

    def get_mission_config(self, mission_type: str) -> dict:
        """Get the full mission config dict including stages."""
        self.ensure_loaded()
        return self._missions.get(mission_type, {})

    def get_state_machine(self, mission_type: str) -> type[StateMachine] | None:
        """Get the state machine class for validation."""
        self.ensure_loaded()
        return self._state_machines.get(mission_type)

    def validate_transition(self, mission_type: str, from_state: str, to_state: str) -> bool:
        """Check if a transition is valid for the given mission."""
        self.ensure_loaded()
        mdef = self._missions.get(mission_type, {})
        for t in mdef.get("transitions", []):
            if t["from"] == from_state and t["to"] == to_state:
                return True
        return False

    def get_transition_guard(self, mission_type: str, from_state: str, to_state: str) -> str | None:
        """Get the guard name for a specific transition, or None."""
        self.ensure_loaded()
        mdef = self._missions.get(mission_type, {})
        for t in mdef.get("transitions", []):
            if t["from"] == from_state and t["to"] == to_state:
                return t.get("guard")
        return None

    def get_default_config(self, mission_type: str) -> dict:
        """Get default_config for a mission (merged with task-level config)."""
        self.ensure_loaded()
        return self._missions.get(mission_type, {}).get("default_config", {})

    def get_agent_config(self, agent_key: str) -> dict | None:
        """Get agent config by lowercase key."""
        self.ensure_loaded()
        return self._agents.get(agent_key.lower())

    def get_all_agent_configs(self) -> dict[str, dict]:
        """Get all agent configs."""
        self.ensure_loaded()
        return dict(self._agents)

    def get_agent_configs_as_legacy(self) -> dict[str, dict]:
        """Return configs in the old AGENT_CONFIGS format for backward compat."""
        self.ensure_loaded()
        legacy = {}
        for key, cfg in self._agents.items():
            legacy[key] = {
                "name": cfg["name"],
                "role": cfg["role"],
                "session_key": f"agent:{key}:main",
                "mcp_servers": cfg.get("mcp_servers", []),
                "heartbeat_offset": cfg.get("heartbeat_offset", 0),
                "level": cfg.get("level", "specialist"),
            }
            if "agent_class" in cfg:
                legacy[key]["agent_class"] = cfg["agent_class"]
            if "always_run" in cfg:
                legacy[key]["always_run"] = cfg["always_run"]
            if "heartbeat_interval" in cfg:
                legacy[key]["heartbeat_interval"] = cfg["heartbeat_interval"]
        return legacy

    def list_missions(self) -> list[dict]:
        """List all missions with metadata."""
        self.ensure_loaded()
        return [
            {
                "name": mname,
                "description": mdef.get("description", ""),
                "transitions": mdef.get("transitions", []),
                "guards": [
                    t["guard"] for t in mdef.get("transitions", []) if t.get("guard")
                ],
            }
            for mname, mdef in self._missions.items()
        ]

    def validate_yaml(self, data: dict) -> list[str]:
        """Validate a workflow YAML dict. Returns list of errors (empty = valid).

        Deep validation based on learnings from heartbeat integration tests:
        - All transition states must exist in TaskStatus enum
        - initial_state must have outgoing transitions
        - Every non-terminal mission must have a path to DONE
        - state_agents roles must reference agent roles defined in the same YAML
        - state_agents must cover all reachable states (except DONE)
        - post_check values must be recognized by GenericMission
        - pre/post action types must be registered
        - Agent heartbeat offsets should not collide within same mission
        - Pipeline staggering advice for multi-agent missions
        """
        errors: list[str] = []
        warnings: list[str] = []

        if "missions" not in data:
            errors.append("Missing 'missions' section")
        if "agents" not in data:
            errors.append("Missing 'agents' section")
        if errors:
            return errors  # can't validate further

        # Build lookup: role → [agent keys]
        role_to_agents: dict[str, list[str]] = {}
        mission_to_agents: dict[str, list[dict]] = {}
        valid_missions = set(data.get("missions", {}).keys())

        for akey, adef in data.get("agents", {}).items():
            mission = adef.get("mission")
            if mission and mission not in valid_missions:
                errors.append(f"Agent '{akey}' references unknown mission '{mission}'")
            if "name" not in adef:
                errors.append(f"Agent '{akey}' missing 'name'")
            if "role" not in adef:
                errors.append(f"Agent '{akey}' missing 'role'")
            role = adef.get("role", "")
            role_to_agents.setdefault(role, []).append(akey)
            if mission:
                mission_to_agents.setdefault(mission, []).append(adef)

        # Import TaskStatus to validate states
        try:
            from mission_control.mission_control.core.database import TaskStatus
            valid_statuses = {s.name for s in TaskStatus}
        except ImportError:
            valid_statuses = None

        for mname, mdef in data.get("missions", {}).items():
            prefix = f"Mission '{mname}'"

            # --- Transitions ---
            transitions = mdef.get("transitions", [])
            if not transitions:
                errors.append(f"{prefix} has no transitions")
                continue

            all_states: set[str] = set()
            from_states: set[str] = set()
            to_states: set[str] = set()

            for i, t in enumerate(transitions):
                if "from" not in t or "to" not in t:
                    errors.append(f"{prefix} transition {i} missing from/to")
                    continue
                frm, to = t["from"], t["to"]
                all_states.update([frm, to])
                from_states.add(frm)
                to_states.add(to)

                # Guard exists?
                guard = t.get("guard")
                if guard and not GuardRegistry.get(guard):
                    errors.append(
                        f"{prefix} transition {i} ({frm}→{to}): "
                        f"unknown guard '{guard}'"
                    )

            # --- States in TaskStatus enum ---
            if valid_statuses:
                for s in all_states:
                    if s not in valid_statuses:
                        errors.append(
                            f"{prefix}: state '{s}' not in TaskStatus enum. "
                            f"Valid: {sorted(valid_statuses)}"
                        )

            # --- initial_state ---
            initial = mdef.get("initial_state", "ASSIGNED")
            if initial not in from_states and initial != "DONE":
                errors.append(
                    f"{prefix}: initial_state '{initial}' has no outgoing transitions. "
                    f"States with outgoing transitions: {sorted(from_states)}"
                )

            # --- Reachability: path to DONE ---
            verify_strategy = mdef.get("verify_strategy", "none")
            terminal_states = all_states - from_states
            if "DONE" not in terminal_states and "DONE" not in all_states:
                if verify_strategy == "pr" and "REVIEW" in terminal_states:
                    pass  # Build-style: REVIEW terminal, verify handles DONE
                else:
                    errors.append(
                        f"{prefix}: no path to DONE. Terminal states: "
                        f"{sorted(terminal_states)}. Add a transition to DONE "
                        f"from one of these states, or set verify_strategy: pr "
                        f"to delegate completion to the verify mission."
                    )

            # --- state_agents validation ---
            state_agents = mdef.get("state_agents", {})
            if state_agents:
                non_done_states = all_states - {"DONE"}
                uncovered = non_done_states - set(state_agents.keys())
                if uncovered:
                    errors.append(
                        f"{prefix}: state_agents missing coverage for states: "
                        f"{sorted(uncovered)}. Tasks reaching these states "
                        f"won't be reassigned to any agent."
                    )

                for state, role in state_agents.items():
                    if state not in all_states and state != "DONE":
                        warnings.append(
                            f"{prefix}: state_agents references state '{state}' "
                            f"which is not reachable via transitions"
                        )
                    agents_with_role = role_to_agents.get(role, [])
                    if not agents_with_role:
                        errors.append(
                            f"{prefix}: state_agents[{state}] = '{role}' but no agent "
                            f"has this role. Available roles: "
                            f"{sorted(set(r for r in role_to_agents if r))}"
                        )
                    elif len(agents_with_role) > 1:
                        warnings.append(
                            f"{prefix}: state_agents[{state}] = '{role}' matches "
                            f"multiple agents: {agents_with_role}. First match will "
                            f"be used for reassignment."
                        )

            # --- Stages validation ---
            stages = mdef.get("stages", {})
            for stage_name, stage_cfg in stages.items():
                if stage_name not in all_states:
                    warnings.append(
                        f"{prefix}: stage config for '{stage_name}' but this state "
                        f"is not reachable via transitions"
                    )

                pc = stage_cfg.get("post_check")
                if pc and pc not in _VALID_POST_CHECKS:
                    errors.append(
                        f"{prefix} stage '{stage_name}': unknown post_check "
                        f"'{pc}'. Valid: {sorted(_VALID_POST_CHECKS)}"
                    )

                for action_key in ("pre_actions", "post_actions"):
                    for j, acfg in enumerate(stage_cfg.get(action_key, [])):
                        action_name = acfg.get("action", "")
                        if action_name and action_name not in _ACTION_HANDLERS:
                            errors.append(
                                f"{prefix} stage '{stage_name}' {action_key}[{j}]: "
                                f"unknown action '{action_name}'. "
                                f"Registered: {sorted(_ACTION_HANDLERS.keys())}"
                            )

            # --- Heartbeat staggering (warnings only) ---
            agents = mission_to_agents.get(mname, [])
            if len(agents) > 1:
                offsets = [
                    (a.get("name", "?"), a.get("heartbeat_offset", 0))
                    for a in agents
                ]
                seen_offsets: dict[int, str] = {}
                for name, offset in offsets:
                    if offset in seen_offsets:
                        warnings.append(
                            f"{prefix}: agents '{seen_offsets[offset]}' and "
                            f"'{name}' have the same heartbeat_offset={offset}. "
                            f"Stagger offsets to avoid thundering herd."
                        )
                    seen_offsets[offset] = name

        # Append warnings as soft errors (prefixed for filtering)
        for w in warnings:
            errors.append(f"[warning] {w}")

        return errors

    def reload(self):
        """Hot-reload: re-read YAML and rebuild everything.

        Atomic: if loading fails, the previous valid state is preserved.
        """
        old_missions = self._missions
        old_agents = self._agents
        old_machines = self._state_machines
        old_loaded = self._loaded
        self._loaded = False
        self._missions = {}
        self._agents = {}
        self._state_machines = {}
        try:
            self.load()
        except Exception:
            self._missions = old_missions
            self._agents = old_agents
            self._state_machines = old_machines
            self._loaded = old_loaded
            raise

    def to_dict(self) -> dict:
        """Export current config as dict (for GET /workflow)."""
        self.ensure_loaded()
        return {
            "version": "1.0",
            "missions": self._missions,
            "agents": self._agents,
        }


# ── Module-level singleton ───────────────────────────────────────
_loader = WorkflowLoader()


def get_workflow_loader() -> WorkflowLoader:
    """Get the global WorkflowLoader singleton."""
    return _loader
