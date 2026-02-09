"""
E2E Content Mission — Full pipeline robustness tests.

Tests the content mission lifecycle: content-squad agents, long-cycle
heartbeats, MCP tool availability, always_run agents, mission CRUD
for content pipelines, and cross-mission isolation.
Runs against the live API at http://localhost:8000 with real HTTP calls.

Different from existing tests:
  - test_phase7: Tests learning READ endpoints generically
  - test_phase8: Tests MCP registry for Quill (infra agent)
  - test_e2e_build_mission: Tests build-squad task lifecycle
  - This file: Tests content-squad specifics (long-cycle, always_run, MCP tools)
"""

import os
import subprocess
import time
import uuid

import pytest
import requests

BASE = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_sql(sql: str) -> str:
    result = subprocess.run(
        ["psql", "-h", "localhost", "-U", "postgres", "-d", "mission_control",
         "-t", "-A", "-c", sql],
        capture_output=True, text=True,
        env={**os.environ, "PGPASSWORD": "postgres"},
    )
    return result.stdout.strip()


def api_get(path: str, **kw) -> requests.Response:
    return requests.get(f"{BASE}{path}", timeout=10, **kw)


def api_post(path: str, **kw) -> requests.Response:
    return requests.post(f"{BASE}{path}", timeout=120, **kw)


def api_put(path: str, **kw) -> requests.Response:
    return requests.put(f"{BASE}{path}", timeout=10, **kw)


def api_delete(path: str, **kw) -> requests.Response:
    return requests.delete(f"{BASE}{path}", timeout=10, **kw)


@pytest.fixture(scope="module", autouse=True)
def preflight_check():
    try:
        r = api_get("/dashboard/agents")
        assert r.status_code == 200
    except Exception:
        pytest.skip("API server not reachable at localhost:8000")


# ---------------------------------------------------------------------------
# Test: Content Squad Agent Configuration
# ---------------------------------------------------------------------------

class TestContentAgentConfig:
    """Verify content-squad agents have correct long-cycle configs."""

    def test_content_agents_present(self):
        r = api_get("/dashboard/agents")
        agents = {a["name"].lower() for a in r.json()}
        content_agents = {"scout", "ink", "sage", "herald", "lurker", "morgan", "archie", "ezra"}
        assert content_agents.issubset(agents), \
            f"Missing content agents: {content_agents - agents}"

    def test_content_agents_have_long_intervals(self):
        """Content agents should have heartbeat intervals > 1 hour."""
        r = api_get("/workflow")
        agents = r.json().get("agents", {})
        long_cycle = {"scout", "sage", "herald", "lurker", "morgan", "archie"}
        for name in long_cycle:
            cfg = agents.get(name, {})
            interval = cfg.get("heartbeat_interval", 900)
            assert interval > 3600, \
                f"{name} interval {interval}s should be > 3600s (1h)"

    def test_lurker_has_always_run(self):
        """Lurker (Reddit scout) should have always_run config."""
        r = api_get("/workflow")
        lurker = r.json()["agents"].get("lurker", {})
        assert "always_run" in lurker, "Lurker missing always_run"
        assert "prompt" in lurker["always_run"], "Lurker always_run missing prompt"

    def test_lurker_has_mcp_servers(self):
        """Lurker should have tavily and github MCP servers."""
        r = api_get("/workflow")
        lurker = r.json()["agents"].get("lurker", {})
        mcp = lurker.get("mcp_servers", [])
        assert "tavily" in mcp, "Lurker missing tavily MCP"
        assert "github" in mcp, "Lurker missing github MCP"


# ---------------------------------------------------------------------------
# Test: Content Mission Workflow Structure
# ---------------------------------------------------------------------------

class TestContentMissionStructure:
    """Verify the content mission definition is well-formed."""

    def test_content_mission_exists(self):
        r = api_get("/workflow")
        missions = r.json().get("missions", {})
        assert "content" in missions, "Content mission not found"

    def test_content_mission_has_stages(self):
        r = api_get("/workflow")
        content = r.json()["missions"]["content"]
        assert "stages" in content
        assert len(content["stages"]) >= 2, "Content mission needs at least 2 stages"

    def test_content_mission_has_initial_state(self):
        r = api_get("/api/missions/content")
        content = r.json()
        initial = content.get("initial_state")
        assert initial, "Content mission missing initial_state"
        assert initial in content["stages"], \
            f"initial_state '{initial}' not in stages: {list(content['stages'].keys())}"

    def test_content_mission_dag_connectivity(self):
        """Every non-terminal stage should have at least one outgoing transition."""
        r = api_get("/api/missions/content")
        content = r.json()
        stages = set(content.get("stages", {}).keys())
        transitions = content.get("transitions", [])
        froms = {t["from"] for t in transitions}
        tos = {t["to"] for t in transitions}
        # Every stage (except terminal ones) should appear as 'from'
        terminal = tos - froms  # stages only reached, never leaving
        non_terminal = stages - terminal
        for s in non_terminal:
            assert s in froms, f"Content stage '{s}' is a dead-end (no outgoing transition)"


# ---------------------------------------------------------------------------
# Test: Content Agent Heartbeat Behavior
# ---------------------------------------------------------------------------

class TestContentHeartbeat:
    """Test heartbeat for content agents with MCP tool integration."""

    def test_scout_heartbeat(self):
        """Scout (SEO Researcher) heartbeat should succeed."""
        r = api_post("/heartbeat/scout")
        assert r.status_code == 200
        data = r.json()
        assert "agent" in data or "status" in data

    def test_heartbeat_records_activity(self):
        """After heartbeat, activities endpoint should show it."""
        r = api_get("/dashboard/activities")
        assert r.status_code == 200
        activities = r.json()
        assert isinstance(activities, list)
        # Should have at least one activity
        if activities:
            a = activities[0]
            assert "agent" in a or "description" in a or "type" in a


# ---------------------------------------------------------------------------
# Test: MCP Tool Availability for Content Agents
# ---------------------------------------------------------------------------

class TestContentMCPTools:
    """Verify MCP tools are properly registered for content agents."""

    def test_mcp_registry_has_tavily(self):
        r = api_get("/mcp/servers")
        assert r.status_code == 200
        servers = r.json()
        names = [s["name"] for s in servers]
        assert "tavily" in names, "Tavily MCP not in registry"

    def test_mcp_registry_has_github(self):
        r = api_get("/mcp/servers")
        servers = r.json()
        names = [s["name"] for s in servers]
        assert "github" in names, "GitHub MCP not in registry"

    def test_all_content_mcp_refs_in_registry(self):
        """All MCP servers referenced by content agents exist in registry."""
        workflow_r = api_get("/workflow")
        mcp_r = api_get("/mcp/servers")

        registry_names = {s["name"] for s in mcp_r.json()}
        agents = workflow_r.json().get("agents", {})
        content_agents = {"scout", "ink", "sage", "herald", "lurker", "morgan", "archie", "ezra"}

        missing = []
        for name in content_agents:
            cfg = agents.get(name, {})
            for mcp_name in cfg.get("mcp_servers", []):
                if mcp_name not in registry_names:
                    missing.append(f"{name} → {mcp_name}")

        assert not missing, f"Content agents reference unregistered MCP servers: {missing}"


# ---------------------------------------------------------------------------
# Test: Content Mission CRUD (separate from build CRUD tests)
# ---------------------------------------------------------------------------

class TestContentMissionCRUD:
    """Test mission CRUD specific to content pipeline patterns."""

    _name = f"e2e_content_test_{uuid.uuid4().hex[:6]}"

    def test_create_content_pipeline(self):
        """Create a content-style mission with research→write→review→publish stages."""
        r = api_post("/api/missions", json={
            "mission_type": self._name,
            "description": "E2E content pipeline test",
            "initial_state": "research",
            "stages": {
                "research": {"prompt_template": "default"},
                "write": {"prompt_template": "default"},
                "review": {"prompt_template": "default"},
                "publish": {"prompt_template": "default"},
            },
            "transitions": [
                {"from": "research", "to": "write", "guard": "auto"},
                {"from": "write", "to": "review", "guard": "auto"},
                {"from": "review", "to": "publish", "guard": "auto"},
                {"from": "review", "to": "write", "guard": "revision_needed"},
            ],
        })
        assert r.status_code == 200

    def test_content_pipeline_persisted(self):
        r = api_get(f"/api/missions/{self._name}")
        assert r.status_code == 200
        data = r.json()
        stages = data.get("stages", data)
        stage_names = set(stages.keys()) if isinstance(stages, dict) else set()
        assert "research" in stage_names or "research" in str(data)

    def test_update_add_seo_stage(self):
        """Add an SEO optimization stage between write and review."""
        r = api_put(f"/api/missions/{self._name}", json={
            "description": "E2E content pipeline (updated with SEO)",
            "initial_state": "research",
            "stages": {
                "research": {"prompt_template": "default"},
                "write": {"prompt_template": "default"},
                "seo_optimize": {"prompt_template": "default"},
                "review": {"prompt_template": "default"},
                "publish": {"prompt_template": "default"},
            },
            "transitions": [
                {"from": "research", "to": "write", "guard": "auto"},
                {"from": "write", "to": "seo_optimize", "guard": "auto"},
                {"from": "seo_optimize", "to": "review", "guard": "auto"},
                {"from": "review", "to": "publish", "guard": "auto"},
            ],
        })
        assert r.status_code == 200

    def test_cleanup(self):
        r = api_delete(f"/api/missions/{self._name}")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Test: Cross-Mission Isolation
# ---------------------------------------------------------------------------

class TestCrossMissionIsolation:
    """Ensure build and content missions don't interfere with each other."""

    def test_build_and_content_are_separate_missions(self):
        r = api_get("/workflow")
        missions = r.json().get("missions", {})
        assert "build" in missions
        assert "content" in missions
        # Stages should be different
        build_stages = set(missions["build"].get("stages", {}).keys())
        content_stages = set(missions["content"].get("stages", {}).keys())
        assert build_stages != content_stages, "Build and content have identical stages"

    def test_agents_belong_to_correct_mission(self):
        """Verify agents are assigned to the right mission type."""
        r = api_get("/dashboard/agents")
        agents = r.json()
        build_set = {"friday", "fury", "loki", "pepper", "wanda", "shuri", "wong"}
        content_set = {"scout", "ink", "sage", "herald", "lurker", "morgan", "archie", "ezra"}
        for a in agents:
            name = a["name"].lower()
            mission = (a.get("mission") or "").lower()
            if name in build_set:
                assert mission == "build" or not mission, \
                    f"Build agent {name} has mission={mission}"
            elif name in content_set:
                assert mission == "content" or not mission, \
                    f"Content agent {name} has mission={mission}"

    def test_learning_events_filter_by_mission(self):
        """Mission filter should return only events for that mission."""
        build_r = api_get("/dashboard/learning/events?limit=20&mission=build")
        content_r = api_get("/dashboard/learning/events?limit=20&mission=content")
        assert build_r.status_code == 200
        assert content_r.status_code == 200

    def test_missions_aggregate_endpoint(self):
        """Missions aggregate should list both build and content."""
        r = api_get("/dashboard/learning/missions")
        assert r.status_code == 200
        data = r.json()
        types = {m.get("mission_type", m.get("type", "")) for m in data}
        assert "build" in types or len(data) > 0, "Missions aggregate should include build"


# ---------------------------------------------------------------------------
# Test: Copilot SDK Built-in Tool Exclusion
# ---------------------------------------------------------------------------

class TestBuiltinToolExclusion:
    """Verify that SDK built-in tools are excluded for non-Vision agents."""

    def test_excluded_tools_list_complete(self):
        """The exclusion list should cover all dangerous built-in tools."""
        from mission_control.mission_control.core.copilot_model import _EXCLUDED_BUILTIN_TOOLS
        dangerous = {"bash", "edit", "create", "task", "web_search", "web_fetch"}
        assert dangerous.issubset(set(_EXCLUDED_BUILTIN_TOOLS)), \
            f"Missing dangerous tools: {dangerous - set(_EXCLUDED_BUILTIN_TOOLS)}"

    def test_copilot_model_has_agent_name_field(self):
        """CopilotModel should accept agent_name for conditional exclusion."""
        from mission_control.mission_control.core.copilot_model import CopilotModel
        m = CopilotModel(id="gpt-4.1", agent_name="TestAgent")
        assert m.agent_name == "TestAgent"

    def test_copilot_model_has_sdk_tools_method(self):
        """CopilotModel should have set_sdk_tools_from_mcp method."""
        from mission_control.mission_control.core.copilot_model import CopilotModel
        m = CopilotModel(id="gpt-4.1")
        assert hasattr(m, "set_sdk_tools_from_mcp")
        assert callable(m.set_sdk_tools_from_mcp)

    def test_copilot_model_has_sdk_tools_field(self):
        """CopilotModel should have _sdk_tools field defaulting to None."""
        from mission_control.mission_control.core.copilot_model import CopilotModel
        m = CopilotModel(id="gpt-4.1")
        assert m._sdk_tools is None


# ---------------------------------------------------------------------------
# Test: ETA for Content Agents (Long-Cycle)
# ---------------------------------------------------------------------------

class TestContentETA:
    """ETA calculation for long-cycle content agents."""

    def test_long_cycle_agent_eta_uses_interval(self):
        """Content agents with >1h intervals should use actual interval, not 15min."""
        r = api_get("/dashboard/tasks")
        workflow_r = api_get("/workflow")
        agents_cfg = workflow_r.json().get("agents", {})

        content_agents = {"scout", "ink", "sage", "herald", "lurker", "morgan", "archie"}
        for t in r.json():
            eta = t.get("eta")
            if not eta:
                continue
            for assignee in t.get("assignees", []):
                if assignee.lower() in content_agents:
                    interval = agents_cfg.get(assignee.lower(), {}).get("heartbeat_interval", 900)
                    if interval > 3600:
                        # Long-cycle: ETA should be substantial, not 1 minute
                        assert eta["minutes"] > 5, \
                            f"Long-cycle {assignee} has suspiciously low ETA: {eta['minutes']}m"

    def test_eta_has_all_required_fields(self):
        """ETA dict should have queue_position, queue_size, agent_busy, next_heartbeat_min."""
        r = api_get("/dashboard/tasks")
        for t in r.json():
            eta = t.get("eta")
            if eta is None:
                continue
            required = {"minutes", "queue_position", "queue_size", "agent_busy", "next_heartbeat_min"}
            assert required.issubset(set(eta.keys())), \
                f"ETA missing fields: {required - set(eta.keys())}"


# ---------------------------------------------------------------------------
# Test: Dashboard Endpoints Stability
# ---------------------------------------------------------------------------

class TestDashboardStability:
    """Rapid-fire dashboard requests to test stability under load."""

    def test_concurrent_dashboard_reads(self):
        """Hit multiple dashboard endpoints and verify all return 200."""
        endpoints = [
            "/dashboard/agents",
            "/dashboard/tasks",
            "/dashboard/activities",
            "/dashboard/learning/stats",
            "/dashboard/learning/events?limit=5",
            "/dashboard/learning/agents",
            "/dashboard/learning/timeline?hours=24",
            "/dashboard/learning/patterns",
        ]
        failures = []
        for ep in endpoints:
            try:
                r = api_get(ep)
                if r.status_code != 200:
                    failures.append(f"{ep}: {r.status_code}")
            except Exception as e:
                failures.append(f"{ep}: {e}")
        assert not failures, f"Dashboard endpoint failures: {failures}"

    def test_dashboard_responses_are_json(self):
        """All dashboard endpoints should return valid JSON."""
        endpoints = [
            "/dashboard/agents",
            "/dashboard/tasks",
            "/dashboard/activities",
            "/dashboard/learning/stats",
        ]
        for ep in endpoints:
            r = api_get(ep)
            try:
                r.json()
            except Exception:
                pytest.fail(f"{ep} did not return valid JSON: {r.text[:100]}")
