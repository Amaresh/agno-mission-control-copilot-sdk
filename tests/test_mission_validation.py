"""
Mission Validation Tests — ensures workflows.yaml validation catches all known
footguns discovered during heartbeat integration testing.

Tests validate that bad mission configs are BLOCKED at load time, not silently
accepted and only discovered at runtime when agents crash or get stuck.

Run: pytest tests/test_mission_validation.py -v
"""

import copy
import pytest
import yaml

from mission_control.mission_control.core.workflow_loader import WorkflowLoader


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_yaml():
    """Minimal valid workflows.yaml with a working content-style pipeline."""
    return {
        "missions": {
            "test_pipeline": {
                "description": "Test content pipeline",
                "initial_state": "RESEARCH",
                "verify_strategy": "none",
                "transitions": [
                    {"from": "RESEARCH", "guard": "has_research", "to": "DRAFT"},
                    {"from": "DRAFT", "guard": "has_draft", "to": "REVIEW"},
                    {"from": "REVIEW", "guard": "quality_approved", "to": "PUBLISH"},
                    {"from": "PUBLISH", "guard": "is_published", "to": "DONE"},
                ],
                "state_agents": {
                    "RESEARCH": "Researcher",
                    "DRAFT": "Writer",
                    "REVIEW": "Editor",
                    "PUBLISH": "Publisher",
                },
                "stages": {
                    "RESEARCH": {
                        "prompt_template": "research",
                        "pre_actions": [{"action": "tavily_search", "query": "{title}"}],
                        "post_actions": [{"action": "github_commit", "message": "research"}],
                    },
                    "DRAFT": {"prompt_template": "draft"},
                    "REVIEW": {"post_check": "review_approved"},
                    "PUBLISH": {},
                },
            },
        },
        "agents": {
            "alpha": {"name": "Alpha", "role": "Researcher", "mission": "test_pipeline",
                      "heartbeat_offset": 0},
            "beta": {"name": "Beta", "role": "Writer", "mission": "test_pipeline",
                     "heartbeat_offset": 5},
            "gamma": {"name": "Gamma", "role": "Editor", "mission": "test_pipeline",
                      "heartbeat_offset": 10},
            "delta": {"name": "Delta", "role": "Publisher", "mission": "test_pipeline",
                      "heartbeat_offset": 15},
        },
    }


@pytest.fixture
def loader():
    return WorkflowLoader()


def _errors(loader, data):
    """Return only hard errors (not warnings)."""
    return [e for e in loader.validate_yaml(data) if not e.startswith("[warning]")]


def _warnings(loader, data):
    """Return only warnings."""
    return [e.removeprefix("[warning] ") for e in loader.validate_yaml(data) if e.startswith("[warning]")]


# ===========================================================================
# Test: Valid config passes
# ===========================================================================

class TestValidConfig:
    def test_minimal_valid_config_passes(self, loader, base_yaml):
        assert _errors(loader, base_yaml) == []

    def test_build_style_pr_verify_passes(self, loader, base_yaml):
        """Build missions with verify_strategy: pr can stop at REVIEW."""
        base_yaml["missions"]["build_test"] = {
            "description": "Build workflow",
            "initial_state": "ASSIGNED",
            "verify_strategy": "pr",
            "transitions": [
                {"from": "ASSIGNED", "to": "IN_PROGRESS"},
                {"from": "IN_PROGRESS", "guard": "has_open_pr", "to": "REVIEW"},
                {"from": "IN_PROGRESS", "guard": "has_error", "to": "ASSIGNED"},
            ],
            "state_agents": {
                "ASSIGNED": "Developer",
                "IN_PROGRESS": "Developer",
                "REVIEW": "Developer",
            },
        }
        base_yaml["agents"]["dev"] = {
            "name": "Dev", "role": "Developer", "mission": "build_test",
        }
        assert _errors(loader, base_yaml) == []


# ===========================================================================
# Test: Structural errors
# ===========================================================================

class TestStructuralErrors:
    def test_missing_missions_section(self, loader):
        errors = _errors(loader, {"agents": {}})
        assert any("Missing 'missions'" in e for e in errors)

    def test_missing_agents_section(self, loader):
        errors = _errors(loader, {"missions": {}})
        assert any("Missing 'agents'" in e for e in errors)

    def test_mission_with_no_transitions(self, loader, base_yaml):
        base_yaml["missions"]["test_pipeline"]["transitions"] = []
        errors = _errors(loader, base_yaml)
        assert any("no transitions" in e for e in errors)

    def test_transition_missing_from_to(self, loader, base_yaml):
        base_yaml["missions"]["test_pipeline"]["transitions"].append(
            {"guard": "some_guard"}
        )
        errors = _errors(loader, base_yaml)
        assert any("missing from/to" in e for e in errors)


# ===========================================================================
# Test: State validation — states must be in TaskStatus enum
# ===========================================================================

class TestStateValidation:
    def test_unknown_state_in_transition(self, loader, base_yaml):
        """States not in TaskStatus enum are caught."""
        base_yaml["missions"]["test_pipeline"]["transitions"].append(
            {"from": "PUBLISH", "to": "PHANTOM_STATE"}
        )
        errors = _errors(loader, base_yaml)
        assert any("PHANTOM_STATE" in e and "TaskStatus" in e for e in errors)

    def test_initial_state_must_have_outgoing_transition(self, loader, base_yaml):
        """initial_state set to a state with no outgoing transitions."""
        base_yaml["missions"]["test_pipeline"]["initial_state"] = "PUBLISH"
        # PUBLISH has an outgoing transition, so use a state that doesn't
        base_yaml["missions"]["test_pipeline"]["transitions"] = [
            {"from": "RESEARCH", "to": "DRAFT"},
            {"from": "DRAFT", "to": "REVIEW"},
            {"from": "REVIEW", "to": "DONE"},
        ]
        base_yaml["missions"]["test_pipeline"]["initial_state"] = "PUBLISH"
        errors = _errors(loader, base_yaml)
        assert any("initial_state 'PUBLISH'" in e and "no outgoing" in e for e in errors)

    def test_initial_state_nonexistent(self, loader, base_yaml):
        base_yaml["missions"]["test_pipeline"]["initial_state"] = "PHANTOM"
        errors = _errors(loader, base_yaml)
        assert any("initial_state 'PHANTOM'" in e for e in errors)


# ===========================================================================
# Test: Path to DONE — missions must complete
# ===========================================================================

class TestPathToDone:
    def test_no_done_state_is_error(self, loader, base_yaml):
        """Remove the DONE transition — should fail."""
        base_yaml["missions"]["test_pipeline"]["transitions"] = [
            {"from": "RESEARCH", "to": "DRAFT"},
            {"from": "DRAFT", "to": "REVIEW"},
            {"from": "REVIEW", "to": "PUBLISH"},
            # Missing: PUBLISH → DONE
        ]
        errors = _errors(loader, base_yaml)
        assert any("no path to DONE" in e for e in errors)

    def test_verify_pr_strategy_allows_review_terminal(self, loader, base_yaml):
        """verify_strategy: pr means REVIEW can be terminal."""
        base_yaml["missions"]["test_pipeline"]["transitions"] = [
            {"from": "RESEARCH", "to": "DRAFT"},
            {"from": "DRAFT", "to": "REVIEW"},
        ]
        base_yaml["missions"]["test_pipeline"]["verify_strategy"] = "pr"
        errors = _errors(loader, base_yaml)
        assert not any("no path to DONE" in e for e in errors)

    def test_verify_pr_without_review_terminal_still_errors(self, loader, base_yaml):
        """verify_strategy: pr but terminal is not REVIEW — still an error."""
        base_yaml["missions"]["test_pipeline"]["transitions"] = [
            {"from": "RESEARCH", "to": "DRAFT"},
        ]
        base_yaml["missions"]["test_pipeline"]["verify_strategy"] = "pr"
        errors = _errors(loader, base_yaml)
        assert any("no path to DONE" in e for e in errors)


# ===========================================================================
# Test: Guard validation
# ===========================================================================

class TestGuardValidation:
    def test_unknown_guard_is_error(self, loader, base_yaml):
        base_yaml["missions"]["test_pipeline"]["transitions"][0]["guard"] = "nonexistent_guard"
        errors = _errors(loader, base_yaml)
        assert any("unknown guard 'nonexistent_guard'" in e for e in errors)

    def test_known_guard_passes(self, loader, base_yaml):
        """All guards in base_yaml are registered."""
        errors = _errors(loader, base_yaml)
        assert not any("unknown guard" in e for e in errors)


# ===========================================================================
# Test: state_agents validation — the reassignment bug source
# ===========================================================================

class TestStateAgentsValidation:
    def test_state_agents_missing_state_coverage(self, loader, base_yaml):
        """Remove one state from state_agents — should error."""
        del base_yaml["missions"]["test_pipeline"]["state_agents"]["REVIEW"]
        errors = _errors(loader, base_yaml)
        assert any("missing coverage" in e and "REVIEW" in e for e in errors)

    def test_state_agents_role_not_found(self, loader, base_yaml):
        """Role that no agent has — should error."""
        base_yaml["missions"]["test_pipeline"]["state_agents"]["REVIEW"] = "Nonexistent Role"
        errors = _errors(loader, base_yaml)
        assert any("Nonexistent Role" in e and "no agent has this role" in e for e in errors)

    def test_state_agents_role_multiple_agents_warns(self, loader, base_yaml):
        """Multiple agents with same role — warning, not error."""
        base_yaml["agents"]["gamma2"] = {
            "name": "Gamma2", "role": "Editor", "mission": "test_pipeline",
            "heartbeat_offset": 12,
        }
        warnings = _warnings(loader, base_yaml)
        assert any("matches multiple agents" in w and "Editor" in w for w in warnings)
        # But no hard error
        errors = _errors(loader, base_yaml)
        assert not any("Editor" in e for e in errors)

    def test_state_agents_references_unreachable_state_warns(self, loader, base_yaml):
        """state_agents lists a state not in transitions — warning."""
        base_yaml["missions"]["test_pipeline"]["state_agents"]["INBOX"] = "Researcher"
        warnings = _warnings(loader, base_yaml)
        assert any("INBOX" in w and "not reachable" in w for w in warnings)


# ===========================================================================
# Test: Stage config validation
# ===========================================================================

class TestStageValidation:
    def test_unknown_post_check_is_error(self, loader, base_yaml):
        base_yaml["missions"]["test_pipeline"]["stages"]["DRAFT"] = {
            "post_check": "totally_fake_check"
        }
        errors = _errors(loader, base_yaml)
        assert any("unknown post_check 'totally_fake_check'" in e for e in errors)

    def test_valid_post_check_passes(self, loader, base_yaml):
        errors = _errors(loader, base_yaml)
        assert not any("post_check" in e for e in errors)

    def test_unknown_action_in_pre_actions(self, loader, base_yaml):
        base_yaml["missions"]["test_pipeline"]["stages"]["DRAFT"] = {
            "pre_actions": [{"action": "nonexistent_action"}]
        }
        errors = _errors(loader, base_yaml)
        assert any("unknown action 'nonexistent_action'" in e for e in errors)

    def test_unknown_action_in_post_actions(self, loader, base_yaml):
        base_yaml["missions"]["test_pipeline"]["stages"]["DRAFT"] = {
            "post_actions": [{"action": "invalid_action"}]
        }
        errors = _errors(loader, base_yaml)
        assert any("unknown action 'invalid_action'" in e for e in errors)

    def test_valid_actions_pass(self, loader, base_yaml):
        errors = _errors(loader, base_yaml)
        assert not any("unknown action" in e for e in errors)

    def test_stage_for_unreachable_state_warns(self, loader, base_yaml):
        base_yaml["missions"]["test_pipeline"]["stages"]["PROMOTE"] = {
            "prompt_template": "promote"
        }
        warnings = _warnings(loader, base_yaml)
        assert any("PROMOTE" in w and "not reachable" in w for w in warnings)


# ===========================================================================
# Test: Agent ↔ Mission binding
# ===========================================================================

class TestAgentMissionBinding:
    def test_agent_references_unknown_mission(self, loader, base_yaml):
        base_yaml["agents"]["orphan"] = {
            "name": "Orphan", "role": "Nobody", "mission": "nonexistent_mission",
        }
        errors = _errors(loader, base_yaml)
        assert any("unknown mission 'nonexistent_mission'" in e for e in errors)

    def test_agent_missing_name(self, loader, base_yaml):
        base_yaml["agents"]["bad"] = {"role": "Something", "mission": "test_pipeline"}
        errors = _errors(loader, base_yaml)
        assert any("missing 'name'" in e for e in errors)

    def test_agent_missing_role(self, loader, base_yaml):
        base_yaml["agents"]["bad"] = {"name": "Bad", "mission": "test_pipeline"}
        errors = _errors(loader, base_yaml)
        assert any("missing 'role'" in e for e in errors)


# ===========================================================================
# Test: Heartbeat staggering — offset collision
# ===========================================================================

class TestHeartbeatStaggering:
    def test_same_offset_warns(self, loader, base_yaml):
        """Two agents on same mission with same offset → thundering herd warning."""
        base_yaml["agents"]["beta"]["heartbeat_offset"] = 0  # same as alpha
        warnings = _warnings(loader, base_yaml)
        assert any("same heartbeat_offset=0" in w for w in warnings)

    def test_different_offsets_no_warning(self, loader, base_yaml):
        """Properly staggered agents produce no offset warnings."""
        warnings = _warnings(loader, base_yaml)
        assert not any("heartbeat_offset" in w for w in warnings)


# ===========================================================================
# Test: Validation blocks load()
# ===========================================================================

class TestLoadBlocking:
    def test_load_raises_on_hard_error(self, loader, base_yaml, tmp_path):
        """WorkflowLoader.load() raises ValueError for invalid YAML."""
        bad_yaml = copy.deepcopy(base_yaml)
        bad_yaml["missions"]["test_pipeline"]["state_agents"]["REVIEW"] = "Ghost Role"

        yaml_file = tmp_path / "bad_workflows.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(bad_yaml, f)

        with pytest.raises(ValueError, match="error"):
            loader.load(yaml_file)

    def test_load_succeeds_with_warnings_only(self, loader, base_yaml, tmp_path):
        """Load succeeds when only warnings exist (no hard errors)."""
        ok_yaml = copy.deepcopy(base_yaml)
        # Add duplicate-role warning (not a hard error)
        ok_yaml["agents"]["gamma2"] = {
            "name": "Gamma2", "role": "Editor", "mission": "test_pipeline",
            "heartbeat_offset": 12,
        }

        yaml_file = tmp_path / "ok_workflows.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(ok_yaml, f)

        loader.load(yaml_file)  # should not raise
        assert "test_pipeline" in loader._missions


# ===========================================================================
# Test: Real workflows.yaml passes validation
# ===========================================================================

class TestRealWorkflowsYaml:
    def test_current_workflows_yaml_is_valid(self, loader):
        """The actual workflows.yaml in the repo passes validation."""
        from pathlib import Path
        yaml_path = Path(__file__).resolve().parents[1] / "workflows.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        errors = _errors(loader, data)
        assert errors == [], f"Real workflows.yaml has errors: {errors}"
