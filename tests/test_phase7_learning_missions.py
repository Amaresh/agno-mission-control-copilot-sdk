"""
Phase 7 — Dashboard & Learnings Redesign: E2E Tests

Tests all mission-type filtering, per-mission aggregates, and the new
mission_type column across dashboard and learning endpoints.
Runs against the live API at http://localhost:8000 with real HTTP calls.
"""

import asyncio
import os
import subprocess
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


# ═══════════════════════════════════════════════════════════════════════════
# Suite 1 — Dashboard Tasks: mission_type field
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardTasks:
    """GET /dashboard/tasks returns tasks with mission_type field."""

    def test_tasks_returns_list(self):
        r = requests.get(f"{BASE}/dashboard/tasks")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0, "Expected at least one task"

    def test_tasks_have_mission_type_key(self):
        r = requests.get(f"{BASE}/dashboard/tasks")
        data = r.json()
        for task in data[:10]:
            assert "mission_type" in task, f"Task {task.get('id')} missing mission_type"

    def test_tasks_mission_type_non_null(self):
        r = requests.get(f"{BASE}/dashboard/tasks")
        data = r.json()
        for task in data[:10]:
            assert task["mission_type"] is not None, (
                f"Task {task.get('id')} has null mission_type"
            )

    def test_tasks_mission_type_is_string(self):
        r = requests.get(f"{BASE}/dashboard/tasks")
        data = r.json()
        for task in data[:10]:
            assert isinstance(task["mission_type"], str)
            assert len(task["mission_type"]) > 0


# ═══════════════════════════════════════════════════════════════════════════
# Suite 2 — Learning Stats: mission filter
# ═══════════════════════════════════════════════════════════════════════════

class TestLearningStats:
    """GET /dashboard/learning/stats with and without ?mission= filter."""

    EXPECTED_KEYS = {
        "total_events", "by_type", "by_agent", "pattern_count",
        "avg_heartbeat_seconds", "task_success_rate", "task_total",
        "mission_filter",
    }

    def test_stats_returns_expected_keys(self):
        r = requests.get(f"{BASE}/dashboard/learning/stats")
        assert r.status_code == 200
        data = r.json()
        assert self.EXPECTED_KEYS.issubset(data.keys()), (
            f"Missing keys: {self.EXPECTED_KEYS - data.keys()}"
        )

    def test_stats_with_mission_filter(self):
        r = requests.get(f"{BASE}/dashboard/learning/stats", params={"mission": "build"})
        assert r.status_code == 200
        data = r.json()
        assert data["mission_filter"] == "build"

    def test_stats_without_filter_includes_all(self):
        r = requests.get(f"{BASE}/dashboard/learning/stats")
        data = r.json()
        assert data["mission_filter"] is None
        assert data["total_events"] > 0, "Expected events in unfiltered stats"

    def test_stats_nonexistent_mission_returns_zeros(self):
        r = requests.get(f"{BASE}/dashboard/learning/stats",
                         params={"mission": "nonexistent_xyz_999"})
        assert r.status_code == 200
        data = r.json()
        assert data["total_events"] == 0
        assert data["by_type"] == {}
        assert data["mission_filter"] == "nonexistent_xyz_999"


# ═══════════════════════════════════════════════════════════════════════════
# Suite 3 — Learning Timeline: mission filter
# ═══════════════════════════════════════════════════════════════════════════

class TestLearningTimeline:
    """GET /dashboard/learning/timeline with and without ?mission= filter."""

    def test_timeline_returns_expected_shape(self):
        r = requests.get(f"{BASE}/dashboard/learning/timeline")
        assert r.status_code == 200
        data = r.json()
        assert "hours" in data
        assert "data" in data
        assert "mission_filter" in data

    def test_timeline_mission_filter_echoed(self):
        r = requests.get(f"{BASE}/dashboard/learning/timeline",
                         params={"mission": "build"})
        data = r.json()
        assert data["mission_filter"] == "build"

    def test_timeline_without_filter_has_data(self):
        r = requests.get(f"{BASE}/dashboard/learning/timeline")
        data = r.json()
        assert data["mission_filter"] is None
        assert isinstance(data["data"], dict)
        # There should be at least some hours with data
        assert len(data["data"]) > 0, "Expected timeline data for recent hours"


# ═══════════════════════════════════════════════════════════════════════════
# Suite 4 — Learning Agents: mission filter
# ═══════════════════════════════════════════════════════════════════════════

class TestLearningAgents:
    """GET /dashboard/learning/agents with and without ?mission= filter."""

    EXPECTED_AGENT_KEYS = {
        "name", "heartbeats", "avg_heartbeat_sec", "last_heartbeat",
        "tasks_total", "tasks_success", "tasks_avg_duration", "errors",
    }

    def test_agents_returns_list_with_expected_keys(self):
        r = requests.get(f"{BASE}/dashboard/learning/agents")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0
        agent = data[0]
        assert self.EXPECTED_AGENT_KEYS.issubset(agent.keys()), (
            f"Missing keys: {self.EXPECTED_AGENT_KEYS - agent.keys()}"
        )

    def test_agents_without_filter_returns_multiple(self):
        r = requests.get(f"{BASE}/dashboard/learning/agents")
        data = r.json()
        assert len(data) >= 5, f"Expected ≥5 agents, got {len(data)}"

    def test_agents_with_mission_filter(self):
        r = requests.get(f"{BASE}/dashboard/learning/agents",
                         params={"mission": "build"})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)


# ═══════════════════════════════════════════════════════════════════════════
# Suite 5 — Learning Events: mission filter + mission_type in response
# ═══════════════════════════════════════════════════════════════════════════

class TestLearningEvents:
    """GET /dashboard/learning/events — response shape and filtering."""

    EXPECTED_EVENT_KEYS = {"id", "agent", "event_type", "context", "created_at"}

    def test_events_returns_list_with_expected_keys(self):
        r = requests.get(f"{BASE}/dashboard/learning/events", params={"limit": 5})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0
        event = data[0]
        assert self.EXPECTED_EVENT_KEYS.issubset(event.keys()), (
            f"Missing keys: {self.EXPECTED_EVENT_KEYS - event.keys()}"
        )

    def test_events_include_mission_type_field(self):
        r = requests.get(f"{BASE}/dashboard/learning/events", params={"limit": 5})
        data = r.json()
        for ev in data:
            assert "mission_type" in ev, f"Event {ev.get('id')} missing mission_type"

    def test_events_mission_build_filter(self):
        """Existing data has NULL mission_type, so ?mission=build returns 0."""
        r = requests.get(f"{BASE}/dashboard/learning/events",
                         params={"mission": "build", "limit": 50})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        # All events in DB have NULL mission_type, so strict filter returns 0
        assert len(data) == 0, (
            "Expected 0 events with mission=build (existing data is NULL)"
        )

    def test_events_limit_param(self):
        r = requests.get(f"{BASE}/dashboard/learning/events", params={"limit": 3})
        assert r.status_code == 200
        data = r.json()
        assert len(data) <= 3


# ═══════════════════════════════════════════════════════════════════════════
# Suite 6 — Learning Patterns: mission filter + mission_type in response
# ═══════════════════════════════════════════════════════════════════════════

class TestLearningPatterns:
    """GET /dashboard/learning/patterns — response shape and filtering."""

    def test_patterns_returns_list_with_mission_type(self):
        r = requests.get(f"{BASE}/dashboard/learning/patterns")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0
        for p in data[:5]:
            assert "mission_type" in p, f"Pattern {p.get('id')} missing mission_type"

    def test_patterns_with_mission_filter(self):
        r = requests.get(f"{BASE}/dashboard/learning/patterns",
                         params={"mission": "build"})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_patterns_without_filter_returns_all(self):
        r = requests.get(f"{BASE}/dashboard/learning/patterns")
        data = r.json()
        assert len(data) > 0, "Expected at least one pattern"


# ═══════════════════════════════════════════════════════════════════════════
# Suite 7 — Missions Aggregate Endpoint
# ═══════════════════════════════════════════════════════════════════════════

class TestMissionsAggregate:
    """GET /dashboard/learning/missions — per-mission-type aggregates."""

    EXPECTED_KEYS = {
        "mission_type", "total_events", "tasks_total", "tasks_success",
        "task_success_rate", "patterns_count", "patterns_avg_confidence",
    }

    def test_missions_returns_list(self):
        r = requests.get(f"{BASE}/dashboard/learning/missions")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_missions_item_has_expected_keys(self):
        r = requests.get(f"{BASE}/dashboard/learning/missions")
        data = r.json()
        for item in data:
            assert self.EXPECTED_KEYS.issubset(item.keys()), (
                f"Missing keys: {self.EXPECTED_KEYS - item.keys()}"
            )

    def test_missions_build_type_exists(self):
        """NULL mission_type data is COALESCE'd to 'build'."""
        r = requests.get(f"{BASE}/dashboard/learning/missions")
        data = r.json()
        mission_types = [m["mission_type"] for m in data]
        assert "build" in mission_types, (
            f"Expected 'build' mission type, got: {mission_types}"
        )

    def test_missions_task_success_rate_range(self):
        r = requests.get(f"{BASE}/dashboard/learning/missions")
        data = r.json()
        for item in data:
            rate = item["task_success_rate"]
            if rate is not None:
                assert 0 <= rate <= 1, (
                    f"task_success_rate {rate} out of range for {item['mission_type']}"
                )


# ═══════════════════════════════════════════════════════════════════════════
# Suite 8 — Capture Functions: insert events with mission_type via DB
# ═══════════════════════════════════════════════════════════════════════════

class TestCaptureWithMissionType:
    """Insert learning data with mission_type via SQL, verify via API, then clean up."""

    # Unique tag to avoid collision with real data
    TAG = f"e2e_{uuid.uuid4().hex[:8]}"

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        """Remove test rows after every test method."""
        yield
        run_sql(
            f"DELETE FROM learning_events WHERE mission_type = '{self.TAG}';"
        )
        run_sql(
            f"DELETE FROM learning_patterns WHERE mission_type = '{self.TAG}';"
        )

    def _insert_event(self, event_type: str = "heartbeat") -> str:
        eid = str(uuid.uuid4())
        run_sql(
            f"INSERT INTO learning_events (id, event_type, mission_type, context, processed, created_at) "
            f"VALUES ('{eid}', '{event_type}', '{self.TAG}', "
            f"'{{\"test\": true}}'::jsonb, false, now());"
        )
        return eid

    def _insert_pattern(self) -> str:
        pid = str(uuid.uuid4())
        run_sql(
            f"INSERT INTO learning_patterns "
            f"(id, type, mission_type, trigger_text, context, resolution, confidence, occurrence_count, created_at, updated_at) "
            f"VALUES ('{pid}', 'TOOL_USAGE', '{self.TAG}', 'E2E test pattern', "
            f"'{{\"test\": true}}'::jsonb, '{{\"action\": \"none\"}}'::jsonb, "
            f"0.75, 1, now(), now());"
        )
        return pid

    def test_insert_event_appears_in_api(self):
        self._insert_event("task_outcome")
        r = requests.get(f"{BASE}/dashboard/learning/events",
                         params={"mission": self.TAG, "limit": 10})
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1, f"Expected ≥1 event with mission={self.TAG}"
        assert data[0]["mission_type"] == self.TAG

    def test_insert_event_appears_in_missions(self):
        self._insert_event("heartbeat")
        r = requests.get(f"{BASE}/dashboard/learning/missions")
        assert r.status_code == 200
        data = r.json()
        mission_types = [m["mission_type"] for m in data]
        assert self.TAG in mission_types, (
            f"Expected '{self.TAG}' in missions, got: {mission_types}"
        )

    def test_insert_pattern_appears_in_api(self):
        self._insert_pattern()
        r = requests.get(f"{BASE}/dashboard/learning/patterns",
                         params={"mission": self.TAG})
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1, f"Expected ≥1 pattern with mission={self.TAG}"
        assert data[0]["mission_type"] == self.TAG

    def test_stats_reflect_inserted_event(self):
        self._insert_event("tool_usage")
        r = requests.get(f"{BASE}/dashboard/learning/stats",
                         params={"mission": self.TAG})
        assert r.status_code == 200
        data = r.json()
        assert data["total_events"] >= 1
        assert data["mission_filter"] == self.TAG

    def test_cleanup_removes_test_data(self):
        """Verify cleanup actually works (insert → cleanup → verify gone)."""
        self._insert_event("heartbeat")
        # Cleanup runs via fixture, so manually trigger
        run_sql(f"DELETE FROM learning_events WHERE mission_type = '{self.TAG}';")
        r = requests.get(f"{BASE}/dashboard/learning/events",
                         params={"mission": self.TAG, "limit": 10})
        data = r.json()
        assert len(data) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Suite 9 — Guard Monitor API
# ═══════════════════════════════════════════════════════════════════════════

class TestGuardMonitor:
    """Import and exercise guard_monitor functions."""

    def test_guard_monitor_importable(self):
        from mission_control.mission_control.learning.guard_monitor import (
            get_recent_blocks,
            record_guard_block,
        )
        assert callable(record_guard_block)
        assert callable(get_recent_blocks)

    def test_record_and_get_blocks(self):
        from mission_control.mission_control.learning.guard_monitor import (
            _block_log,
            get_recent_blocks,
            record_guard_block,
        )

        tag = f"test_{uuid.uuid4().hex[:6]}"
        # Record a block (async function, run in event loop)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(record_guard_block(
                mission_type=tag,
                from_state="queued",
                to_state="running",
                guard_name="test_guard",
                agent_name="TestAgent",
            ))
        finally:
            loop.close()

        blocks = get_recent_blocks(mission_type=tag)
        assert len(blocks) >= 1
        b = blocks[0]
        assert b["mission_type"] == tag
        assert b["guard"] == "test_guard"
        assert b["transition"] == "queued→running"

        # Clean up: remove our test entries from the in-memory log
        _block_log[:] = [e for e in _block_log if e["mission_type"] != tag]
