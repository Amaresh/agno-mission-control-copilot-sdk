"""
ActionRunner — pluggable pre/post action dispatcher for GenericMission.

Built-in actions:
  - tavily_search: Web search via Tavily API
  - github_read:   Read file from GitHub repo
  - github_commit: Commit content to GitHub repo
  - ensure_branch: Create git branch if missing

Custom actions can be registered via @register_action decorator.
"""

import base64
import os
from typing import Any, Callable, Coroutine

import httpx
import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------
_ACTION_HANDLERS: dict[str, Callable[..., Coroutine]] = {}


def register_action(name: str):
    """Decorator to register a new action handler."""
    def decorator(fn):
        _ACTION_HANDLERS[name] = fn
        return fn
    return decorator


class ActionRunner:
    """Dispatches pre/post actions defined in workflows.yaml stage configs."""

    def __init__(self, task_vars: dict[str, str]):
        """task_vars: template variables like {task_id}, {title}, {owner}, etc."""
        self.task_vars = task_vars

    def _render(self, template: str) -> str:
        """Replace {variable} placeholders with task_vars values."""
        result = template
        for key, value in self.task_vars.items():
            result = result.replace(f"{{{key}}}", str(value))
        return result

    async def run(self, action_cfg: dict, extra_vars: dict | None = None) -> Any:
        """Execute a single action from its YAML config dict.

        action_cfg example:
            {"action": "tavily_search", "query": "{title} motorcycle", "max_results": 5}

        Returns the action result (string for reads/searches, bool for writes).
        """
        action_name = action_cfg.get("action", "")
        handler = _ACTION_HANDLERS.get(action_name)
        if not handler:
            logger.warning("Unknown action", action=action_name)
            return None

        # Merge extra vars (e.g. llm_output) into task_vars for rendering
        merged = {**self.task_vars, **(extra_vars or {})}
        old_vars = self.task_vars
        self.task_vars = merged
        try:
            return await handler(self, action_cfg)
        finally:
            self.task_vars = old_vars

    async def run_all(
        self, actions: list[dict], extra_vars: dict | None = None,
    ) -> dict[str, Any]:
        """Run a list of actions, returning {action_name: result} for each."""
        results: dict[str, Any] = {}
        for cfg in actions:
            name = cfg.get("action", "unknown")
            results[name] = await self.run(cfg, extra_vars=extra_vars)
        return results


# ---------------------------------------------------------------------------
# Built-in actions
# ---------------------------------------------------------------------------

def _github_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }


@register_action("tavily_search")
async def _tavily_search(runner: ActionRunner, cfg: dict) -> str:
    """Web search via Tavily API. Returns formatted markdown results."""
    query = runner._render(cfg.get("query", "{title}"))
    max_results = cfg.get("max_results", 5)
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        return "(Tavily API key not configured — using task description as context)"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": max_results,
                    "include_answer": True,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            parts = []
            if data.get("answer"):
                parts.append(f"**Summary:** {data['answer']}\n")
            for r in data.get("results", []):
                parts.append(
                    f"- [{r.get('title', 'Untitled')}]({r.get('url', '')})\n"
                    f"  {r.get('content', '')[:300]}\n"
                )
            return "\n".join(parts) or "(No results found)"
    except Exception as e:
        logger.warning("Tavily search failed", error=str(e))
        return f"(Search failed: {e})"


@register_action("github_read")
async def _github_read(runner: ActionRunner, cfg: dict) -> str:
    """Read a file from a GitHub repository."""
    owner = runner._render(cfg.get("owner", "{owner}"))
    repo = runner._render(cfg.get("repo", "{repo}"))
    path = runner._render(cfg.get("path", ""))
    ref = runner._render(cfg.get("ref", "main"))
    headers = _github_headers()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                headers=headers,
                params={"ref": ref},
            )
            if resp.status_code == 200:
                content = resp.json().get("content", "")
                return base64.b64decode(content).decode("utf-8")
            return f"(File not found: {path})"
    except Exception as e:
        return f"(Read failed: {e})"


@register_action("github_commit")
async def _github_commit(runner: ActionRunner, cfg: dict) -> bool:
    """Create or update a file on GitHub. Uses llm_output as content by default."""
    owner = runner._render(cfg.get("owner", "{owner}"))
    repo = runner._render(cfg.get("repo", "{repo}"))
    path = runner._render(cfg.get("path", ""))
    branch = runner._render(cfg.get("branch", "main"))
    message = runner._render(cfg.get("message", "content: {title}"))
    content = runner.task_vars.get(
        cfg.get("content_source", "llm_output"), ""
    )
    headers = _github_headers()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Check if file exists (get sha for update)
            sha = None
            existing = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                headers=headers,
                params={"ref": branch},
            )
            if existing.status_code == 200:
                sha = existing.json().get("sha")

            body: dict[str, Any] = {
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "branch": branch,
            }
            if sha:
                body["sha"] = sha

            resp = await client.put(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                headers=headers,
                json=body,
            )
            if resp.status_code in (200, 201):
                logger.info("Pushed file to GitHub", path=path)
                return True
            logger.warning(
                "GitHub push failed",
                status=resp.status_code, body=resp.text[:200],
            )
    except Exception as e:
        logger.error("GitHub push error", error=str(e))
    return False


@register_action("ensure_branch")
async def _ensure_branch(runner: ActionRunner, cfg: dict) -> bool:
    """Create a git branch on GitHub if it doesn't exist."""
    repo_name = runner._render(cfg.get("repository", "{owner}/{repo}"))
    branch = runner._render(cfg.get("branch", "{branch_name}"))
    base = runner._render(cfg.get("base", "main"))
    headers = _github_headers()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_name}/branches/{branch}",
                headers=headers,
            )
            if resp.status_code == 200:
                return True
            base_resp = await client.get(
                f"https://api.github.com/repos/{repo_name}/git/ref/heads/{base}",
                headers=headers,
            )
            if base_resp.status_code == 200:
                base_sha = base_resp.json()["object"]["sha"]
                create_resp = await client.post(
                    f"https://api.github.com/repos/{repo_name}/git/refs",
                    headers=headers,
                    json={"ref": f"refs/heads/{branch}", "sha": base_sha},
                )
                return create_resp.status_code == 201
    except Exception as e:
        logger.warning("Branch creation failed", error=str(e))
    return False
