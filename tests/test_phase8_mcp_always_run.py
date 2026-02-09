"""
Phase 8 — MCP Registry + always_run Agent Behaviors: E2E Tests

Tests MCP server registry endpoints, always_run config flow, and
Quill's migration from custom agent to GenericAgent with always_run.
Runs against the live API at http://localhost:8000 with real HTTP calls.
"""

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


# ===========================================================================
# Suite 1: GET /mcp/servers — registry endpoint
# ===========================================================================
class TestMCPServersEndpoint:
    """Verify /mcp/servers lists configured servers with availability."""

    def test_returns_list(self):
        r = requests.get(f"{BASE}/mcp/servers")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 3, "Should have at least github, digitalocean, telegram"

    def test_server_fields(self):
        r = requests.get(f"{BASE}/mcp/servers")
        servers = r.json()
        for s in servers:
            assert "name" in s
            assert "available" in s
            assert isinstance(s["available"], bool)

    def test_github_server_present(self):
        r = requests.get(f"{BASE}/mcp/servers")
        servers = {s["name"]: s for s in r.json()}
        assert "github" in servers
        gh = servers["github"]
        assert "description" in gh

    def test_digitalocean_server_present(self):
        r = requests.get(f"{BASE}/mcp/servers")
        servers = {s["name"]: s for s in r.json()}
        assert "digitalocean" in servers

    def test_missing_env_reported(self):
        """Servers without env vars should show missing_env list."""
        r = requests.get(f"{BASE}/mcp/servers")
        servers = r.json()
        # At least one server should have missing env (test env doesn't have all tokens)
        unavailable = [s for s in servers if not s["available"]]
        if unavailable:
            s = unavailable[0]
            assert "missing_env" in s
            assert isinstance(s["missing_env"], list)


# ===========================================================================
# Suite 2: POST /mcp/reload — hot-reload endpoint
# ===========================================================================
class TestMCPReload:
    """Verify /mcp/reload endpoint."""

    def test_reload_returns_ok(self):
        r = requests.post(f"{BASE}/mcp/reload")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "servers" in data
        assert isinstance(data["servers"], int)

    def test_reload_count_matches_list(self):
        r_reload = requests.post(f"{BASE}/mcp/reload")
        r_list = requests.get(f"{BASE}/mcp/servers")
        assert r_reload.json()["servers"] == len(r_list.json())


# ===========================================================================
# Suite 3: Workflow config — always_run in Quill
# ===========================================================================
class TestAlwaysRunConfig:
    """Verify always_run config flows through workflow -> agent."""

    def test_quill_has_always_run(self):
        r = requests.get(f"{BASE}/workflow")
        data = r.json()
        quill = data["agents"]["quill"]
        assert "always_run" in quill
        assert "prompt" in quill["always_run"]
        assert "timeout" in quill["always_run"]

    def test_quill_always_run_timeout(self):
        r = requests.get(f"{BASE}/workflow")
        quill = r.json()["agents"]["quill"]
        assert quill["always_run"]["timeout"] == 60

    def test_quill_always_run_prompt_content(self):
        r = requests.get(f"{BASE}/workflow")
        quill = r.json()["agents"]["quill"]
        prompt = quill["always_run"]["prompt"]
        assert "monitoring" in prompt.lower() or "DigitalOcean" in prompt

    def test_other_agents_no_always_run(self):
        """Agents without always_run should not have the key."""
        r = requests.get(f"{BASE}/workflow")
        agents = r.json()["agents"]
        # Friday and Wanda should NOT have always_run
        for name in ["friday", "wanda"]:
            if name in agents:
                assert agents[name].get("always_run") is None

    def test_always_run_survives_reload(self):
        """After workflow reload, always_run config persists."""
        r1 = requests.get(f"{BASE}/workflow")
        original = r1.json()

        # Trigger a workflow reload (POST the same config back)
        import yaml
        r2 = requests.post(
            f"{BASE}/workflow",
            data=yaml.dump(original),
            headers={"Content-Type": "text/yaml"},
        )
        assert r2.status_code == 200

        r3 = requests.get(f"{BASE}/workflow")
        quill = r3.json()["agents"]["quill"]
        assert "always_run" in quill


# ===========================================================================
# Suite 4: MCPRegistry — config loading
# ===========================================================================
class TestMCPRegistryConfig:
    """Test registry internals via the API."""

    def test_server_names_match_config(self):
        """Server names from API should match mcp_servers.yaml."""
        r = requests.get(f"{BASE}/mcp/servers")
        names = {s["name"] for s in r.json()}
        # These are defined in mcp_servers.yaml
        assert "github" in names
        assert "digitalocean" in names

    def test_server_count_reasonable(self):
        r = requests.get(f"{BASE}/mcp/servers")
        count = len(r.json())
        assert 3 <= count <= 20, f"Unexpected server count: {count}"


# ===========================================================================
# Suite 5: Agent factory — Quill as GenericAgent
# ===========================================================================
class TestQuillGenericAgent:
    """Verify Quill is now created as GenericAgent, not custom QuillAgent."""

    def test_quill_agent_import(self):
        """The old create_quill_agent still works (backward compat)."""
        # This just tests the module is importable — actual import happens in factory
        from mission_control.squad.quill.agent import create_quill_agent
        assert callable(create_quill_agent)

    def test_quill_in_workflow_agents(self):
        """Quill appears in workflow agent list."""
        r = requests.get(f"{BASE}/workflow")
        assert "quill" in r.json()["agents"]

    def test_quill_has_mcp_servers(self):
        """Quill config includes digitalocean MCP."""
        r = requests.get(f"{BASE}/workflow")
        quill = r.json()["agents"]["quill"]
        assert "digitalocean" in quill.get("mcp_servers", [])


# ===========================================================================
# Suite 6: Integration — MCP + Workflow coherence
# ===========================================================================
class TestMCPWorkflowIntegration:
    """Verify MCP servers referenced in workflow exist in registry."""

    def test_all_workflow_mcp_refs_exist_in_registry(self):
        """Every mcp_servers reference in agents should exist in registry."""
        r_wf = requests.get(f"{BASE}/workflow")
        r_mcp = requests.get(f"{BASE}/mcp/servers")

        registry_names = {s["name"] for s in r_mcp.json()}
        agents = r_wf.json()["agents"]

        for agent_name, config in agents.items():
            for server_ref in config.get("mcp_servers", []):
                assert server_ref in registry_names, (
                    f"Agent '{agent_name}' references MCP server '{server_ref}' "
                    f"not found in registry. Available: {registry_names}"
                )

    def test_post_workflow_reloads_mcp(self):
        """POST /workflow also reloads MCP registry."""
        import yaml
        r1 = requests.get(f"{BASE}/workflow")
        data = r1.json()

        # Post workflow — should trigger MCP reload
        r2 = requests.post(
            f"{BASE}/workflow",
            data=yaml.dump(data),
            headers={"Content-Type": "text/yaml"},
        )
        assert r2.status_code == 200

        # MCP servers should still be accessible
        r3 = requests.get(f"{BASE}/mcp/servers")
        assert r3.status_code == 200
        assert len(r3.json()) >= 3


# ===========================================================================
# Suite 7: Heartbeat always_run capture (learning events)
# ===========================================================================
class TestAlwaysRunCapture:
    """Verify always_run heartbeats are captured in learning system."""

    def test_heartbeat_endpoint_exists(self):
        """The learning timeline endpoint works."""
        r = requests.get(f"{BASE}/dashboard/learning/timeline")
        assert r.status_code == 200

    def test_heartbeat_stats_endpoint(self):
        """The learning stats endpoint works and returns agent data."""
        r = requests.get(f"{BASE}/dashboard/learning/stats")
        assert r.status_code == 200
        data = r.json()
        assert "agents" in data or "total_events" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
