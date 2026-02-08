"""
PR-existence check for review-gating.

Before a task may transition to REVIEW, we verify that an open pull request
exists in the *correct* target repository (extracted from the task description).
"""

import re
from typing import Optional, Tuple

import httpx
import structlog

from agents.config import settings

logger = structlog.get_logger()

_REPO_RE = re.compile(r"Repository:\s*(\S+)", re.IGNORECASE)


def extract_target_repo(description: Optional[str]) -> Optional[str]:
    """Return 'owner/repo' from a task description, or None."""
    if not description:
        return None
    m = _REPO_RE.search(description)
    return m.group(1).strip() if m else None


async def has_open_pr(repo: str, head_prefix: str) -> Tuple[bool, Optional[str]]:
    """Check GitHub for an open PR whose head branch starts with *head_prefix*.

    Returns (True, pr_html_url) if found, else (False, None).
    """
    token = settings.github_token
    if not token:
        logger.warning("No github_token configured — skipping PR check")
        return True, None  # fail-open when no token

    owner_repo = repo  # e.g. "owner/repo-name"
    url = f"https://api.github.com/repos/{owner_repo}/pulls"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params={"state": "open", "per_page": 100},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            for pr in resp.json():
                ref = pr["head"]["ref"]
                if head_prefix and ref.startswith(head_prefix):
                    return True, pr.get("html_url")
    except Exception as e:
        logger.error("PR check failed — allowing transition", error=str(e))
        return True, None  # fail-open on network errors

    return False, None


async def has_open_pr_for_task(repo: str, task_id_short: str) -> Tuple[bool, Optional[str]]:
    """Check GitHub for an open PR whose branch contains *task_id_short*.

    Branches follow convention: {agent_name}/{task_id[:8]}
    Returns (True, pr_html_url) if found, else (False, None).
    """
    if not task_id_short:
        return False, None

    token = settings.github_token
    if not token:
        logger.warning("No github_token configured — skipping PR check")
        return True, None

    url = f"https://api.github.com/repos/{repo}/pulls"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params={"state": "open", "per_page": 100},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            for pr in resp.json():
                ref = pr["head"]["ref"]
                if task_id_short in ref:
                    return True, pr.get("html_url")
    except Exception as e:
        logger.error("PR check failed — allowing transition", error=str(e))
        return True, None

    return False, None
