"""
Guard registry — named boolean checks used by workflow transitions.

Guards are referenced by name in workflows.yaml and evaluated at runtime
before a state transition is allowed. Each guard receives a context dict
with task info and an async DB session, returns True to allow the transition.
"""

from typing import Callable, Awaitable

import structlog

logger = structlog.get_logger()


# Type: async (context, session) -> bool
GuardFn = Callable[[dict, any], Awaitable[bool]]


class GuardRegistry:
    """Maps guard names from workflows.yaml to callable checks."""

    _guards: dict[str, GuardFn] = {}

    @classmethod
    def register(cls, name: str):
        """Decorator to register a guard function."""
        def decorator(fn: GuardFn):
            cls._guards[name] = fn
            return fn
        return decorator

    @classmethod
    def get(cls, name: str) -> GuardFn | None:
        return cls._guards.get(name)

    @classmethod
    def list_guards(cls) -> list[str]:
        return sorted(cls._guards.keys())

    @classmethod
    async def check(cls, name: str, context: dict, session=None) -> bool:
        """Evaluate a named guard. Returns True if guard passes or doesn't exist."""
        fn = cls._guards.get(name)
        if fn is None:
            logger.warning("Unknown guard, allowing transition", guard=name)
            return True
        try:
            return await fn(context, session)
        except Exception as e:
            logger.error("Guard evaluation failed", guard=name, error=str(e))
            return False


# ── Built-in Guards ──────────────────────────────────────────────

@GuardRegistry.register("has_open_pr")
async def _has_open_pr(context: dict, session=None) -> bool:
    """True if an open PR exists for the task's branch."""
    from mission_control.mission_control.core.pr_check import has_open_pr
    repo = context.get("repository", "")
    head_prefix = context.get("head_prefix", "")
    if not repo or not head_prefix:
        return False
    found, _ = await has_open_pr(repo, head_prefix)
    return found


@GuardRegistry.register("no_open_pr")
async def _no_open_pr(context: dict, session=None) -> bool:
    """True if NO open PR exists (inverse of has_open_pr)."""
    return not await _has_open_pr(context, session)


@GuardRegistry.register("has_branch")
async def _has_branch(context: dict, session=None) -> bool:
    """True if the task's branch exists in the target repo."""
    import httpx
    from mission_control.config import settings
    repo = context.get("repository", "")
    branch = context.get("branch_name", "")
    if not repo or not branch:
        return False
    token = settings.github_token
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/branches/{branch}",
                headers=headers,
            )
            return resp.status_code == 200
    except Exception:
        return False


@GuardRegistry.register("has_error")
async def _has_error(context: dict, session=None) -> bool:
    """True if the last agent response contained an error indicator."""
    response = context.get("last_response", "")
    if not response:
        return False
    return any(err in response.lower() for err in [
        "broken pipe", "cannot proceed", "error:", "timeout",
        "connection refused", "rate limit",
    ])


@GuardRegistry.register("is_stale")
async def _is_stale(context: dict, session=None) -> bool:
    """True if the task has been in its current state beyond the threshold."""
    from datetime import datetime, timezone
    updated_at = context.get("updated_at")
    threshold_minutes = context.get("stale_threshold_minutes", 90)
    if not updated_at:
        return False
    if isinstance(updated_at, str):
        updated_at = datetime.fromisoformat(updated_at)
    age_minutes = (datetime.now(timezone.utc) - updated_at).total_seconds() / 60
    return age_minutes > threshold_minutes


@GuardRegistry.register("files_changed_ok")
async def _files_changed_ok(context: dict, session=None) -> bool:
    """True if the PR diff is below the max files threshold."""
    import httpx
    from mission_control.config import settings
    repo = context.get("repository", "")
    head_prefix = context.get("head_prefix", "")
    max_files = context.get("max_files", 500)
    if not repo or not head_prefix:
        return True  # permissive default
    token = settings.github_token
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/pulls?state=open&head={head_prefix}",
                headers=headers,
            )
            if resp.status_code == 200:
                prs = resp.json()
                if prs:
                    return prs[0].get("changed_files", 0) <= max_files
    except Exception:
        pass
    return True
