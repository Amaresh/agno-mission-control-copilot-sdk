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

logger = structlog.get_logger()

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

    def get_all_mission_states(self) -> list[str]:
        """Collect custom states from all missions in config."""
        self.ensure_loaded()
        states = []
        for mdef in self._missions.values():
            states.extend(mdef.get("states", []))
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
        """Validate a workflow YAML dict. Returns list of errors (empty = valid)."""
        errors = []
        if "missions" not in data:
            errors.append("Missing 'missions' section")
        if "agents" not in data:
            errors.append("Missing 'agents' section")

        for mname, mdef in data.get("missions", {}).items():
            if not mdef.get("transitions"):
                errors.append(f"Mission '{mname}' has no transitions")
            for i, t in enumerate(mdef.get("transitions", [])):
                if "from" not in t or "to" not in t:
                    errors.append(f"Mission '{mname}' transition {i} missing from/to")
                if t.get("guard") and not GuardRegistry.get(t["guard"]):
                    errors.append(f"Mission '{mname}' transition {i}: unknown guard '{t['guard']}'")

        valid_missions = set(data.get("missions", {}).keys())
        for akey, adef in data.get("agents", {}).items():
            mission = adef.get("mission")
            if mission and mission not in valid_missions:
                errors.append(f"Agent '{akey}' references unknown mission '{mission}'")
            if "name" not in adef:
                errors.append(f"Agent '{akey}' missing 'name'")
            if "role" not in adef:
                errors.append(f"Agent '{akey}' missing 'role'")

        return errors

    def reload(self):
        """Hot-reload: re-read YAML and rebuild everything."""
        self._loaded = False
        self._missions = {}
        self._agents = {}
        self._state_machines = {}
        self.load()

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
