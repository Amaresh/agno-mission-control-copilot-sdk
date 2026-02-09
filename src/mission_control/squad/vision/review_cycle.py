"""
TEMPORARY: Copilot code review cycle for Vision Healer.

Every heartbeat:
1. List all open PRs in Amaresh/mission-control-review
2. Request Copilot review on each
3. Wait 5 min for reviews to generate
4. Fetch new review comments, deduplicate against existing tasks
5. Create MC tasks for genuinely new comments
6. If new-task rate drops to <= MERGE_THRESHOLD, close batch review PRs
   (review/* branches) — PR #1 already has everything.

Remove this module once initial code review is complete.
"""

import asyncio
import re
from datetime import datetime, timezone
from typing import List

import httpx
import structlog

from mission_control.config import settings
from mission_control.mission_control.core.database import (
    AsyncSessionLocal,
    Task,
    TaskStatus,
    TaskPriority,
    TaskAssignment,
    Agent as AgentModel,
)
from dataclasses import dataclass, field
from typing import Optional as _Opt


@dataclass
class HealthCheckResult:
    """Mirror of checks.HealthCheckResult to avoid circular import."""
    name: str
    passed: bool
    message: str
    fix_applied: _Opt[str] = None
    severity: str = "info"

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REVIEW_REPO = "Amaresh/mission-control-review"
REVIEW_WAIT_SECONDS = 300          # 5 minutes
MERGE_THRESHOLD = 2                # <= this many new tasks → start merging
WORKER_AGENTS = ["Friday", "Shuri", "Wong", "Loki", "Wanda", "Pepper"]
COMMENT_ID_TAG = "review_comment_id"   # embedded in task description for dedup

# Comments shorter than this or matching these patterns are skipped
MIN_COMMENT_LEN = 20
SKIP_PATTERNS = ["lgtm", "looks good", "nice work", "great job", "👍", "nit:", "nitpick"]


def _gh_headers():
    return {
        "Authorization": f"token {settings.github_token}",
        "Accept": "application/vnd.github+json",
    }


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

async def _list_open_prs(client: httpx.AsyncClient) -> list[dict]:
    """List all open PRs in the review repo."""
    prs = []
    page = 1
    while True:
        resp = await client.get(
            f"https://api.github.com/repos/{REVIEW_REPO}/pulls",
            headers=_gh_headers(),
            params={"state": "open", "per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        prs.extend(batch)
        page += 1
    return prs


async def _prs_without_copilot_review(
    client: httpx.AsyncClient, prs: list[dict],
) -> list[int]:
    """Return PR numbers that have zero review comments from Copilot.

    NOTE: Copilot code review CANNOT be requested via the REST API.
    "copilot" is a GitHub App, not a user — the `requested_reviewers`
    endpoint silently ignores it.  Auto-review requires a GitHub
    Ruleset (needs Pro for private repos) or manual UI trigger.
    This helper identifies PRs that still need a manual review request.
    """
    unreviewed: list[int] = []
    for pr in prs:
        pr_num = pr["number"]
        resp = await client.get(
            f"https://api.github.com/repos/{REVIEW_REPO}/pulls/{pr_num}/comments",
            headers=_gh_headers(),
            params={"per_page": 1},
        )
        if resp.status_code == 200 and len(resp.json()) == 0:
            unreviewed.append(pr_num)
    return unreviewed


async def _notify_review_needed(pr_numbers: list[int]) -> None:
    """Send Telegram alert asking user to manually request Copilot review."""
    if not pr_numbers:
        return
    from mission_control.squad.vision.notify import send_telegram

    pr_links = "\n".join(
        f"  • [PR #{n}](https://github.com/{REVIEW_REPO}/pull/{n})"
        for n in pr_numbers
    )
    msg = (
        "🔍 *Copilot Review Needed*\n\n"
        f"These PRs in `{REVIEW_REPO}` have no review comments yet.\n"
        "Please open each and click *Re-request review → Copilot* in the UI:\n\n"
        f"{pr_links}\n\n"
        "_Copilot review cannot be triggered via API — requires manual UI or a Ruleset (Pro)._"
    )
    await send_telegram(msg)


async def _fetch_review_comments(client: httpx.AsyncClient, pr_number: int) -> list[dict]:
    """Fetch all review comments (paginated) on a PR."""
    comments: list[dict] = []
    page = 1
    while True:
        resp = await client.get(
            f"https://api.github.com/repos/{REVIEW_REPO}/pulls/{pr_number}/comments",
            headers=_gh_headers(),
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for c in batch:
            c["_pr_number"] = pr_number
        comments.extend(batch)
        page += 1
    return comments


async def _close_pr(client: httpx.AsyncClient, pr_number: int) -> bool:
    """Close a PR without merging."""
    try:
        resp = await client.patch(
            f"https://api.github.com/repos/{REVIEW_REPO}/pulls/{pr_number}",
            headers=_gh_headers(),
            json={"state": "closed"},
        )
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _existing_comment_ids(session) -> set[int]:
    """Return the set of GitHub review-comment IDs already tracked as tasks."""
    from sqlalchemy import select

    stmt = select(Task.description).where(
        Task.description.like(f"%{COMMENT_ID_TAG}:%"),
    )
    result = await session.execute(stmt)
    ids: set[int] = set()
    pattern = re.compile(rf"{COMMENT_ID_TAG}:(\d+)")
    for (desc,) in result:
        if desc:
            for m in pattern.finditer(desc):
                ids.add(int(m.group(1)))
    return ids


async def _get_vision_agent(session) -> AgentModel | None:
    from sqlalchemy import select

    stmt = select(AgentModel).where(AgentModel.name == "Vision")
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_worker_agents(session) -> list[AgentModel]:
    from sqlalchemy import select

    stmt = select(AgentModel).where(AgentModel.name.in_(WORKER_AGENTS))
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Main check function
# ---------------------------------------------------------------------------

async def check_code_reviews() -> List[HealthCheckResult]:
    """
    Copilot code-review cycle (runs at end of every Vision heartbeat).

    Returns HealthCheckResult list for the healer summary.
    """
    results: List[HealthCheckResult] = []

    if not settings.github_token:
        results.append(HealthCheckResult(
            "code_reviews", False, "No GitHub token configured", severity="warning",
        ))
        return results

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # ---- Step 1: list open PRs ----
            open_prs = await _list_open_prs(client)
            if not open_prs:
                results.append(HealthCheckResult(
                    "code_reviews", True, "No open PRs in review repo",
                ))
                return results

            pr_numbers = [pr["number"] for pr in open_prs]
            logger.info("Review cycle: open PRs", prs=pr_numbers)

            # ---- Step 2: notify about PRs needing manual Copilot review ----
            # Copilot review CANNOT be requested via API ("copilot" is a
            # GitHub App, not a user).  Rulesets need Pro for private repos.
            # Instead, detect unreviewed PRs and ping Telegram.
            unreviewed = await _prs_without_copilot_review(client, open_prs)
            if unreviewed:
                await _notify_review_needed(unreviewed)
                logger.info(f"Notified: {len(unreviewed)} PRs need manual Copilot review", prs=unreviewed)
            else:
                logger.info("All open PRs already have review comments")

            # ---- Step 3: wait for reviews to land ----
            logger.info(f"Waiting {REVIEW_WAIT_SECONDS}s for Copilot reviews…")
            await asyncio.sleep(REVIEW_WAIT_SECONDS)

            # ---- Step 4: fetch all review comments ----
            all_comments: list[dict] = []
            for pr_num in pr_numbers:
                all_comments.extend(await _fetch_review_comments(client, pr_num))

            logger.info(f"Fetched {len(all_comments)} total comments across {len(pr_numbers)} PRs")

            # ---- Step 5: deduplicate ----
            # 5a — content-level dedup (same file + body across different PRs)
            seen_content: dict[tuple, dict] = {}
            unique_comments: list[dict] = []
            for c in all_comments:
                key = (c.get("path", ""), c.get("body", "")[:120])
                if key not in seen_content:
                    seen_content[key] = c
                    unique_comments.append(c)

            # 5b — dedup against existing tasks (by GitHub comment ID)
            async with AsyncSessionLocal() as session:
                tracked_ids = await _existing_comment_ids(session)
                new_comments = [
                    c for c in unique_comments
                    if c.get("id") and c["id"] not in tracked_ids
                ]

                # Filter out trivial / praise comments
                actionable: list[dict] = []
                for c in new_comments:
                    body = (c.get("body") or "").strip()
                    if len(body) < MIN_COMMENT_LEN:
                        continue
                    lower = body.lower()
                    if any(skip in lower for skip in SKIP_PATTERNS):
                        continue
                    actionable.append(c)

                logger.info(
                    "Review dedup",
                    total=len(all_comments),
                    unique=len(unique_comments),
                    already_tracked=len(tracked_ids),
                    new=len(new_comments),
                    actionable=len(actionable),
                )

                # ---- Step 6: create tasks ----
                vision = await _get_vision_agent(session)
                workers = await _get_worker_agents(session)
                if not workers:
                    results.append(HealthCheckResult(
                        "code_reviews", False, "No worker agents found for task assignment",
                        severity="warning",
                    ))
                    return results

                creator_id = vision.id if vision else workers[0].id
                created_count = 0

                for idx, comment in enumerate(actionable):
                    file_path = comment.get("path", "unknown")
                    body = comment.get("body", "").strip()
                    pr_num = comment["_pr_number"]
                    line = comment.get("original_line") or comment.get("line") or "?"
                    comment_url = comment.get("html_url", "")
                    diff_hunk = comment.get("diff_hunk", "")
                    comment_id = comment["id"]

                    title = f"[Review PR#{pr_num}] {file_path}"
                    if len(title) > 200:
                        title = title[:197] + "…"

                    description = (
                        f"<!-- {COMMENT_ID_TAG}:{comment_id} -->\n"
                        f"**Copilot Code Review Comment**\n\n"
                        f"**PR:** #{pr_num} in {REVIEW_REPO}\n"
                        f"**File:** `{file_path}` (line {line})\n"
                        f"**Comment URL:** {comment_url}\n\n"
                        f"**Review Comment:**\n{body}\n\n"
                        f"**Code Context:**\n```\n{diff_hunk[-500:] if diff_hunk else 'N/A'}\n```\n\n"
                        f"**Base Branch:** initial-changes\n\n"
                        f"Fix the issue described in the review comment above."
                    )

                    agent = workers[idx % len(workers)]

                    task = Task(
                        title=title,
                        description=description,
                        status=TaskStatus.ASSIGNED,
                        priority=TaskPriority.MEDIUM,
                        created_by_id=creator_id,
                        mission_type="review",
                    )
                    session.add(task)
                    await session.flush()

                    session.add(TaskAssignment(
                        task_id=task.id,
                        agent_id=agent.id,
                    ))
                    created_count += 1

                await session.commit()

            logger.info(f"Created {created_count} review tasks")

            # ---- Step 7: merge check ----
            if created_count <= MERGE_THRESHOLD:
                batch_prs = [
                    pr for pr in open_prs
                    if pr.get("head", {}).get("ref", "").startswith("review/")
                ]
                if batch_prs:
                    logger.info(
                        f"Low task rate ({created_count} <= {MERGE_THRESHOLD}), "
                        f"closing {len(batch_prs)} batch review PRs"
                    )
                    closed = 0
                    for pr in batch_prs:
                        if await _close_pr(client, pr["number"]):
                            closed += 1
                    results.append(HealthCheckResult(
                        "code_reviews", True,
                        f"Review cycle winding down: {created_count} new tasks. "
                        f"Closed {closed}/{len(batch_prs)} batch PRs. "
                        f"PR #1 is the consolidated review PR.",
                        fix_applied=f"Closed {closed} batch review PRs",
                    ))
                else:
                    results.append(HealthCheckResult(
                        "code_reviews", True,
                        f"Review cycle complete: {created_count} new tasks, no batch PRs remaining.",
                    ))
            else:
                results.append(HealthCheckResult(
                    "code_reviews", True,
                    f"Created {created_count} review tasks from {len(actionable)} actionable comments "
                    f"(across {len(pr_numbers)} PRs).",
                ))

    except Exception as e:
        logger.error("Code review cycle failed", error=str(e))
        results.append(HealthCheckResult(
            "code_reviews", False,
            f"Code review cycle failed: {e}",
            severity="warning",
        ))

    return results
