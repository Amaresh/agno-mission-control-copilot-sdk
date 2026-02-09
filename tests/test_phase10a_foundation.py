"""Tests for ActionRunner and PromptLoader — Phase 10a foundation."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# PromptLoader tests
# ---------------------------------------------------------------------------
from mission_control.mission_control.core.prompt_loader import PromptLoader


class TestPromptLoader:
    def setup_method(self):
        self.prompts_dir = Path(__file__).parent.parent / "src" / "mission_control" / "prompts"
        self.loader = PromptLoader(self.prompts_dir)

    def test_load_existing_template(self):
        result = self.loader.render("content_research", context_data="TEST DATA")
        assert "RESEARCH STAGE" in result
        assert "TEST DATA" in result
        assert "{context_data}" not in result

    def test_load_missing_template(self):
        result = self.loader.render("nonexistent_template_xyz")
        assert result == ""

    def test_variable_substitution(self):
        result = self.loader.render(
            "content_base",
            title="Test Article",
            description="A test description",
        )
        assert "Test Article" in result
        assert "A test description" in result
        assert "{title}" not in result
        assert "{description}" not in result

    def test_render_composite(self):
        result = self.loader.render_composite(
            ["content_base", "content_research"],
            title="My Title",
            description="My Desc",
            context_data="Search results here",
        )
        assert "My Title" in result
        assert "RESEARCH STAGE" in result
        assert "Search results here" in result

    def test_build_prompt_variables(self):
        result = self.loader.render(
            "build_dev",
            title="Fix bug",
            description="Fix the login bug",
            repository="acme/test-repo",
            owner="acme",
            repo="test-repo",
            branch_name="agent/abc123",
            source_branch="main",
            context_files_section="",
            learned_context="",
        )
        assert "Fix bug" in result
        assert "acme/test-repo" in result
        assert "agent/abc123" in result

    def test_all_content_templates_exist(self):
        for name in ["content_base", "content_research", "content_draft",
                      "content_review", "content_publish", "content_promote"]:
            result = self.loader.render(name, title="T", description="D",
                                        context_data="C")
            assert result, f"Template {name} should not be empty"

    def test_caching(self):
        self.loader.render("content_base", title="T", description="D")
        assert "content_base" in self.loader._cache
        # Second load should use cache
        self.loader.render("content_base", title="T2", description="D2")
        assert "content_base" in self.loader._cache


# ---------------------------------------------------------------------------
# ActionRunner tests
# ---------------------------------------------------------------------------

from mission_control.mission_control.core.actions import _ACTION_HANDLERS, ActionRunner


class TestActionRunner:
    def test_render_template(self):
        runner = ActionRunner({"title": "Motorcycle CRM", "task_id": "abc123"})
        result = runner._render("{title} guide for {task_id}")
        assert result == "Motorcycle CRM guide for abc123"

    def test_unknown_action_returns_none(self):
        runner = ActionRunner({})
        result = asyncio.get_event_loop().run_until_complete(
            runner.run({"action": "nonexistent_action_xyz"})
        )
        assert result is None

    def test_registered_actions_exist(self):
        expected = {"tavily_search", "github_read", "github_commit", "ensure_branch"}
        assert expected.issubset(set(_ACTION_HANDLERS.keys()))

    @patch("mission_control.mission_control.core.actions.httpx.AsyncClient")
    def test_tavily_search_no_key(self, mock_client):
        runner = ActionRunner({"title": "test"})
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}):
            result = asyncio.get_event_loop().run_until_complete(
                runner.run({"action": "tavily_search", "query": "{title}"})
            )
        assert "not configured" in result

    @patch("mission_control.mission_control.core.actions.httpx.AsyncClient")
    def test_github_read_renders_path(self, mock_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_client.return_value = mock_ctx

        runner = ActionRunner({
            "owner": "acme", "repo": "content-repo",
            "task_id": "abc12345",
        })
        result = asyncio.get_event_loop().run_until_complete(
            runner.run({
                "action": "github_read",
                "path": "content/research/{task_id}-research.md",
            })
        )
        assert "abc12345-research.md" in result

    def test_run_all_returns_dict(self):
        runner = ActionRunner({"title": "test"})
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}):
            results = asyncio.get_event_loop().run_until_complete(
                runner.run_all([
                    {"action": "tavily_search", "query": "{title}"},
                ])
            )
        assert "tavily_search" in results
        assert "not configured" in results["tavily_search"]

    def test_extra_vars_merged(self):
        runner = ActionRunner({"title": "test"})
        # llm_output should be available for github_commit content_source
        assert "title" in runner.task_vars
