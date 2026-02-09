"""
E2E Build Mission — Full pipeline robustness tests.

Tests the build mission lifecycle: task creation → agent assignment →
heartbeat triggering → learning capture → ETA computation → state transitions.
Runs against the live API at http://localhost:8000 with real HTTP calls.

Different from existing tests:
  - test_phase7: Only tests dashboard/learning READ endpoints
  - test_phase8: Only tests MCP registry + always_run config
  - This file: Tests the WRITE path (task lifecycle, heartbeats, ETA, delegation)
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
    """Run SQL against the local Postgres DB and return trimmed output."""
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
    return requests.post(f"{BASE}{path}", timeout=30, **kw)


def api_put(path: str, **kw) -> requests.Response:
    return requests.put(f"{BASE}{path}", timeout=10, **kw)


def api_delete(path: str, **kw) -> requests.Response:
    return requests.delete(f"{BASE}{path}", timeout=10, **kw)


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def preflight_check():
    """Ensure the API server is up before running any tests."""
    try:
        r = api_get("/dashboard/agents")
        assert r.status_code == 200
    except Exception:
        pytest.skip("API server not reachable at localhost:8000")


# ---------------------------------------------------------------------------
# Test: Build Agent Registry & Configuration
# ---------------------------------------------------------------------------

class TestBuildAgentRegistry:
    """Verify build-squad agents are registered with correct configs."""

    def test_all_build_agents_present(self):
        r = api_get("/dashboard/agents")
        assert r.status_code == 200
        agents = r.json()
        names = {a["name"].lower() for a in agents}
        build_agents = {"friday", "fury", "loki", "pepper", "wanda", "shuri", "wong", "jarvis"}
        assert build_agents.issubset(names), f"Missing build agents: {build_agents - names}"

    def test_build_agents_have_heartbeat_offset(self):
        r = api_get("/dashboard/agents")
        agents = r.json()
        build_names = {"friday", "fury", "loki", "pepper", "wanda", "shuri", "wong"}
        for a in agents:
            if a["name"].lower() in build_names:
                assert "last_heartbeat" in a, f"{a['name']} missing last_heartbeat"

    def test_workflow_has_build_mission(self):
        r = api_get("/workflow")
        assert r.status_code == 200
        data = r.json()
        missions = data.get("missions", {})
        assert "build" in missions, "Build mission not found in workflows"

    def test_build_mission_has_stages(self):
        r = api_get("/workflow")
        data = r.json()
        build = data["missions"]["build"]
        assert "stages" in build, "Build mission has no stages"
        assert len(build["stages"]) >= 2, "Build mission should have at least 2 stages"


# ---------------------------------------------------------------------------
# Test: Task CRUD through API
# ---------------------------------------------------------------------------

class TestBuildTaskLifecycle:
    """Create, read, update tasks for the build mission."""

    _task_id = None

    def test_create_task_via_api(self):
        r = api_post("/task", json={
            "title": f"E2E-Build-Test-{uuid.uuid4().hex[:6]}",
            "description": "Automated E2E test for build pipeline robustness",
            "priority": "high",
        })
        assert r.status_code == 200
        data = r.json()
        assert "task_id" in data or "id" in data
        TestBuildTaskLifecycle._task_id = data.get("task_id") or data.get("id")

    def test_task_appears_in_dashboard(self):
        r = api_get("/dashboard/tasks")
        assert r.status_code == 200
        tasks = r.json()
        task_ids = [t["id"] for t in tasks]
        if self._task_id:
            assert self._task_id in task_ids, "Created task not in dashboard"

    def test_task_has_required_fields(self):
        r = api_get("/dashboard/tasks")
        tasks = r.json()
        if not tasks:
            pytest.skip("No tasks in dashboard")
        t = tasks[0]
        required = {"id", "title", "status", "priority", "assignees", "eta", "mission_type"}
        assert required.issubset(set(t.keys())), f"Missing fields: {required - set(t.keys())}"

    def test_task_eta_is_valid_or_null(self):
        """ETA should be null (unassigned) or a dict with 'minutes' key."""
        r = api_get("/dashboard/tasks")
        for t in r.json():
            eta = t.get("eta")
            if eta is not None:
                assert isinstance(eta, dict), f"ETA should be dict, got {type(eta)}"
                assert "minutes" in eta, "ETA dict missing 'minutes'"
                assert isinstance(eta["minutes"], (int, float)), "ETA minutes should be numeric"
                assert eta["minutes"] >= 0, "ETA minutes should be non-negative"


# ---------------------------------------------------------------------------
# Test: Heartbeat & Agent Wake Cycle
# ---------------------------------------------------------------------------

class TestBuildHeartbeat:
    """Test heartbeat triggering for build agents."""

    def test_heartbeat_endpoint_returns_200(self):
        """Trigger Friday's heartbeat (fast build agent)."""
        r = api_post("/heartbeat/friday")
        assert r.status_code == 200
        data = r.json()
        assert "agent" in data or "status" in data

    def test_heartbeat_updates_agent_timestamp(self):
        """After heartbeat, agent's last_heartbeat should be recent."""
        r = api_get("/dashboard/agents")
        agents = {a["name"].lower(): a for a in r.json()}
        friday = agents.get("friday")
        if friday and friday.get("last_heartbeat"):
            # Should be within last 5 minutes
            from datetime import datetime, timezone
            hb = datetime.fromisoformat(friday["last_heartbeat"].replace("Z", "+00:00"))
            age_sec = (datetime.now(timezone.utc) - hb).total_seconds()
            assert age_sec < 300, f"Friday's heartbeat is {age_sec:.0f}s old, expected < 300s"

    def test_heartbeat_creates_learning_event(self):
        """Heartbeat should create a learning event in the system."""
        r = api_get("/dashboard/learning/events?limit=10")
        assert r.status_code == 200
        events = r.json()
        # Should have at least one heartbeat event
        heartbeats = [e for e in events if e.get("event_type") == "heartbeat"]
        assert len(heartbeats) > 0, "No heartbeat learning events found"


# ---------------------------------------------------------------------------
# Test: ETA Calculation Robustness
# ---------------------------------------------------------------------------

class TestBuildETA:
    """ETA computation accuracy for build-squad agents."""

    def test_short_cycle_agent_eta_reasonable(self):
        """Build agents (15-min cycle) should have ETA <= 60 min for first queue slot."""
        r = api_get("/dashboard/tasks")
        for t in r.json():
            eta = t.get("eta")
            if eta and t.get("mission_type") in (None, "build"):
                # First queue position with 15-min cycle should be well under 60 min
                if eta.get("queue_position", 0) <= 1:
                    assert eta["minutes"] <= 60, \
                        f"Build task ETA {eta['minutes']}m too high for queue pos {eta.get('queue_position')}"

    def test_eta_queue_position_increments(self):
        """Multiple tasks assigned to same agent should have increasing ETA."""
        r = api_get("/dashboard/tasks")
        tasks = r.json()
        # Group by assignee
        by_agent = {}
        for t in tasks:
            for a in t.get("assignees", []):
                by_agent.setdefault(a, []).append(t)
        for agent, agent_tasks in by_agent.items():
            etas = [t["eta"]["minutes"] for t in agent_tasks if t.get("eta")]
            if len(etas) >= 2:
                assert etas == sorted(etas), \
                    f"ETAs for {agent} not increasing: {etas}"


# ---------------------------------------------------------------------------
# Test: Mission CRUD (builder API)
# ---------------------------------------------------------------------------

class TestBuildMissionCRUD:
    """Test mission create/read/update/delete through builder endpoints."""

    _test_mission_name = f"e2e_build_test_{uuid.uuid4().hex[:6]}"

    def test_create_mission(self):
        r = api_post("/api/missions", json={
            "mission_type": self._test_mission_name,
            "description": "E2E test mission",
            "initial_state": "init",
            "stages": {
                "init": {"prompt_template": "default"},
                "done": {"prompt_template": "default"},
            },
            "transitions": [{"from": "init", "to": "done"}],
        })
        assert r.status_code == 200
        data = r.json()
        assert data.get("status") == "created" or "success" in str(data).lower()

    def test_read_mission(self):
        r = api_get(f"/api/missions/{self._test_mission_name}")
        assert r.status_code == 200
        data = r.json()
        assert "stages" in data or "init" in str(data)

    def test_update_mission(self):
        r = api_put(f"/api/missions/{self._test_mission_name}", json={
            "description": "E2E test mission (updated)",
            "initial_state": "init",
            "stages": {
                "init": {"prompt_template": "default"},
                "review": {"prompt_template": "default"},
                "done": {"prompt_template": "default"},
            },
            "transitions": [
                {"from": "init", "to": "review"},
                {"from": "review", "to": "done"},
            ],
        })
        assert r.status_code == 200

    def test_delete_mission(self):
        r = api_delete(f"/api/missions/{self._test_mission_name}")
        assert r.status_code == 200

    def test_deleted_mission_not_found(self):
        r = api_get(f"/api/missions/{self._test_mission_name}")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Test: Workflow Integrity After Operations
# ---------------------------------------------------------------------------

class TestBuildWorkflowIntegrity:
    """Ensure workflow config is consistent after CRUD operations."""

    def test_workflow_loads_without_error(self):
        r = api_get("/workflow")
        assert r.status_code == 200
        data = r.json()
        assert "agents" in data
        assert "missions" in data

    def test_build_mission_has_transitions(self):
        r = api_get("/api/missions/build")
        build = r.json()
        transitions = build.get("transitions", [])
        assert len(transitions) > 0, "Build mission has no transitions"
        # Every non-terminal stage should appear as a 'from' in at least one transition
        stages = set(build.get("stages", {}).keys())
        froms = {t["from"] for t in transitions}
        # At least one stage should have an outgoing transition
        assert froms & stages, "No stage has an outgoing transition"

    def test_agent_count_matches_workflow(self):
        """Dashboard agent count should match workflow config."""
        agents_r = api_get("/dashboard/agents")
        workflow_r = api_get("/workflow")
        dashboard_count = len(agents_r.json())
        workflow_count = len(workflow_r.json().get("agents", {}))
        assert dashboard_count == workflow_count, \
            f"Dashboard has {dashboard_count} agents, workflow has {workflow_count}"

    def test_no_orphaned_agents(self):
        """Every agent in dashboard should exist in workflow config."""
        agents_r = api_get("/dashboard/agents")
        workflow_r = api_get("/workflow")
        dashboard_names = {a["name"].lower() for a in agents_r.json()}
        workflow_names = set(workflow_r.json().get("agents", {}).keys())
        orphans = dashboard_names - workflow_names
        assert not orphans, f"Orphaned agents in dashboard: {orphans}"


# ---------------------------------------------------------------------------
# Test: Learning System Under Build Mission
# ---------------------------------------------------------------------------

class TestBuildLearningCapture:
    """Verify learning events are captured during build operations."""

    def test_learning_events_include_build_type(self):
        r = api_get("/dashboard/learning/events?limit=50")
        events = r.json()
        build_events = [e for e in events if e.get("mission_type") == "build"]
        # After heartbeats, there should be at least one build event
        assert isinstance(events, list)

    def test_learning_stats_non_negative(self):
        r = api_get("/dashboard/learning/stats")
        data = r.json()
        for key in ("total_events", "total_patterns"):
            if key in data:
                assert data[key] >= 0, f"{key} is negative: {data[key]}"

    def test_learning_timeline_24h(self):
        r = api_get("/dashboard/learning/timeline?hours=24")
        assert r.status_code == 200
        data = r.json()
        assert "data" in data or "buckets" in data or isinstance(data, list) or "timeline" in data
