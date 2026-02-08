"""
Health check functions for Vision Healer agent.

Each check returns a HealthCheckResult. Checks are deterministic —
no LLM calls. They query the DB, shell, and GitHub API directly.
"""

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import structlog
from sqlalchemy import select, text

from agents.config import settings
from agents.mission_control.core.database import (
    Activity,
    ActivityType,
    AsyncSessionLocal,
    Task,
    TaskStatus,
)

logger = structlog.get_logger()


@dataclass
class HealthCheckResult:
    name: str
    passed: bool
    message: str
    fix_applied: Optional[str] = None
    severity: str = "info"  # info, warning, critical


# ---------------------------------------------------------------------------
# 1. Stale tasks — ASSIGNED/IN_PROGRESS with no activity for >1.5h
# ---------------------------------------------------------------------------
async def check_stale_tasks() -> List[HealthCheckResult]:
    results = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.vision_stale_task_hours)

    async with AsyncSessionLocal() as session:
        stale = await session.execute(
            select(Task).where(
                Task.status.in_([TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS]),
                Task.updated_at < cutoff,
            )
        )
        stale_tasks = stale.scalars().all()

        if not stale_tasks:
            results.append(HealthCheckResult("stale_tasks", True, "No stale tasks"))
            return results

        for task in stale_tasks:
            age_h = (datetime.now(timezone.utc) - task.updated_at).total_seconds() / 3600
            # Fix: move back to INBOX so scheduler can reassign
            old_status = task.status
            task.status = TaskStatus.INBOX
            session.add(Activity(
                type=ActivityType.STATUS_CHANGE,
                task_id=task.id,
                message=f"Vision Healer: stale {old_status.value} task ({age_h:.1f}h), reset to INBOX",
            ))
            results.append(HealthCheckResult(
                "stale_tasks", False,
                f"Task '{task.title[:50]}' stale ({age_h:.1f}h in {old_status.value})",
                fix_applied="Reset to INBOX",
                severity="warning",
            ))

        await session.commit()
    return results


# ---------------------------------------------------------------------------
# 2. Zombie processes — orphaned copilot --headless and GitHub MCP servers
# ---------------------------------------------------------------------------
async def check_zombie_processes() -> List[HealthCheckResult]:
    results = []

    def _count_procs(pattern: str) -> List[int]:
        try:
            out = subprocess.check_output(
                ["ps", "aux"], text=True, timeout=5,
            )
            matching = [line for line in out.strip().split("\n") if pattern in line and "grep" not in line]
            return [int(line.split()[1]) for line in matching if line]
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return []

    # Count expected: 1 per running service that uses Copilot SDK
    mcp_pids = _count_procs("@modelcontextprotocol/server-github")
    copilot_pids = _count_procs("copilot --headless")

    mcp_threshold = 10
    copilot_threshold = 5

    if len(mcp_pids) > mcp_threshold:
        # Kill all but the newest 2
        to_kill = sorted(mcp_pids)[:-2]
        killed = 0
        for pid in to_kill:
            try:
                os.kill(pid, 15)  # SIGTERM
                killed += 1
            except ProcessLookupError:
                pass
        results.append(HealthCheckResult(
            "zombie_mcp", False,
            f"{len(mcp_pids)} GitHub MCP servers (threshold: {mcp_threshold})",
            fix_applied=f"Killed {killed} orphaned processes",
            severity="warning",
        ))
    else:
        results.append(HealthCheckResult(
            "zombie_mcp", True, f"{len(mcp_pids)} GitHub MCP servers (OK)"))

    if len(copilot_pids) > copilot_threshold:
        to_kill = sorted(copilot_pids)[:-2]
        killed = 0
        for pid in to_kill:
            try:
                os.kill(pid, 15)
                killed += 1
            except ProcessLookupError:
                pass
        results.append(HealthCheckResult(
            "zombie_copilot", False,
            f"{len(copilot_pids)} headless copilot processes (threshold: {copilot_threshold})",
            fix_applied=f"Killed {killed} orphaned processes",
            severity="warning",
        ))
    else:
        results.append(HealthCheckResult(
            "zombie_copilot", True, f"{len(copilot_pids)} headless copilot processes (OK)"))

    return results


# ---------------------------------------------------------------------------
# 3. Chatbot responsiveness — ping mc-bot + check last interaction
# ---------------------------------------------------------------------------
async def check_chatbot_health() -> List[HealthCheckResult]:
    results = []

    # Check if mc-bot service is running
    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "is-active", "mc-bot"],
            text=True, timeout=5,
        ).strip()
        bot_active = out == "active"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        bot_active = False

    if not bot_active:
        # Try restart
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", "mc-bot"],
                timeout=10, check=True,
            )
            results.append(HealthCheckResult(
                "chatbot_health", False, "mc-bot was not running",
                fix_applied="Restarted mc-bot service",
                severity="critical",
            ))
        except Exception as e:
            results.append(HealthCheckResult(
                "chatbot_health", False, f"mc-bot down, restart failed: {e}",
                severity="critical",
            ))
        return results

    # Check last human interaction (most recent activity from telegram)
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("""
            SELECT MAX(created_at) as last_activity
            FROM activities
            WHERE message ILIKE '%telegram%' OR message ILIKE '%received%'
        """))
        row = result.fetchone()
        if row and row.last_activity:
            age_h = (datetime.now(timezone.utc) - row.last_activity.replace(
                tzinfo=timezone.utc if row.last_activity.tzinfo is None else row.last_activity.tzinfo
            )).total_seconds() / 3600
            if age_h > 4:
                results.append(HealthCheckResult(
                    "chatbot_health", False,
                    f"No human interaction for {age_h:.1f}h",
                    severity="info",
                ))
            else:
                results.append(HealthCheckResult(
                    "chatbot_health", True,
                    f"Bot active, last interaction {age_h:.1f}h ago"))
        else:
            results.append(HealthCheckResult(
                "chatbot_health", True, "Bot active, no interaction history"))

    return results


# ---------------------------------------------------------------------------
# 4. Service health — all 4 systemd services running
# ---------------------------------------------------------------------------
async def check_service_health() -> List[HealthCheckResult]:
    results = []
    services = [s.strip() for s in settings.vision_monitored_services.split(",") if s.strip()]

    if not services:
        results.append(HealthCheckResult("service_health", True, "No services configured to monitor"))
        return results

    for svc in services:
        try:
            out = subprocess.check_output(
                ["systemctl", "--user", "is-active", svc],
                text=True, timeout=5,
            ).strip()
            if out == "active":
                results.append(HealthCheckResult(f"service_{svc}", True, f"{svc}: active"))
            else:
                subprocess.run(
                    ["systemctl", "--user", "restart", svc],
                    timeout=10, check=True,
                )
                results.append(HealthCheckResult(
                    f"service_{svc}", False, f"{svc} was {out}",
                    fix_applied=f"Restarted {svc}",
                    severity="critical",
                ))
        except Exception as e:
            results.append(HealthCheckResult(
                f"service_{svc}", False, f"{svc} check failed: {e}",
                severity="critical",
            ))

    return results


# ---------------------------------------------------------------------------
# 5. INBOX tasks with assignees — should be ASSIGNED
# ---------------------------------------------------------------------------
async def check_inbox_with_assignees() -> List[HealthCheckResult]:
    results = []

    async with AsyncSessionLocal() as session:
        rows = await session.execute(text("""
            SELECT DISTINCT t.id, t.title
            FROM tasks t
            JOIN task_assignments ta ON ta.task_id = t.id
            WHERE t.status = 'INBOX'
        """))
        stuck = rows.fetchall()

        if not stuck:
            results.append(HealthCheckResult("inbox_assignees", True, "No INBOX tasks with assignees"))
            return results

        for row in stuck:
            await session.execute(
                text("UPDATE tasks SET status = 'ASSIGNED' WHERE id = :id"),
                {"id": row.id},
            )
        await session.commit()

        results.append(HealthCheckResult(
            "inbox_assignees", False,
            f"{len(stuck)} INBOX tasks had assignees",
            fix_applied=f"Transitioned {len(stuck)} tasks to ASSIGNED",
            severity="warning",
        ))

    return results


# ---------------------------------------------------------------------------
# 6. Log file bloat — any log > 50MB
# ---------------------------------------------------------------------------
async def check_log_bloat() -> List[HealthCheckResult]:
    results = []
    log_dir = os.path.join(os.path.dirname(__file__), "../../../logs")
    log_dir = os.path.normpath(log_dir)
    threshold = settings.vision_log_max_mb * 1024 * 1024

    if not os.path.isdir(log_dir):
        results.append(HealthCheckResult("log_bloat", True, "Log directory not found"))
        return results

    for fname in os.listdir(log_dir):
        fpath = os.path.join(log_dir, fname)
        if not os.path.isfile(fpath):
            continue
        size = os.path.getsize(fpath)
        if size > threshold:
            # Truncate to last 500 lines
            try:
                lines = subprocess.check_output(
                    ["tail", "-500", fpath], text=True, timeout=5,
                )
                with open(fpath, "w") as f:
                    f.write(lines)
                results.append(HealthCheckResult(
                    "log_bloat", False,
                    f"{fname}: {size // (1024*1024)}MB",
                    fix_applied="Truncated to last 500 lines",
                    severity="warning",
                ))
            except Exception as e:
                results.append(HealthCheckResult(
                    "log_bloat", False, f"Failed to truncate {fname}: {e}",
                    severity="warning",
                ))

    if not any(r.name == "log_bloat" for r in results):
        results.append(HealthCheckResult("log_bloat", True, f"All logs under {settings.vision_log_max_mb}MB"))

    return results


# ---------------------------------------------------------------------------
# 7. Memory / swap pressure
# ---------------------------------------------------------------------------
async def check_memory_pressure() -> List[HealthCheckResult]:
    results = []

    try:
        out = subprocess.check_output(["free", "-m"], text=True, timeout=5)
        lines = out.strip().split("\n")
        # Mem: total used free shared buff/cache available
        mem_parts = lines[1].split()
        total_mb = int(mem_parts[1])
        available_mb = int(mem_parts[6])
        used_pct = ((total_mb - available_mb) / total_mb) * 100

        swap_parts = lines[2].split()
        swap_total = int(swap_parts[1])
        swap_used = int(swap_parts[2])
        swap_pct = (swap_used / swap_total * 100) if swap_total > 0 else 0

        if used_pct > settings.vision_ram_threshold_pct:
            results.append(HealthCheckResult(
                "memory", False,
                f"RAM: {used_pct:.0f}% used ({available_mb}MB available)",
                severity="critical",
            ))
        else:
            results.append(HealthCheckResult(
                "memory", True, f"RAM: {used_pct:.0f}% ({available_mb}MB available)"))

        if swap_pct > settings.vision_swap_threshold_pct:
            results.append(HealthCheckResult(
                "swap", False,
                f"Swap: {swap_pct:.0f}% used ({swap_used}MB/{swap_total}MB)",
                severity="warning",
            ))
        else:
            results.append(HealthCheckResult(
                "swap", True, f"Swap: {swap_pct:.0f}% ({swap_used}MB/{swap_total}MB)"))

    except Exception as e:
        results.append(HealthCheckResult(
            "memory", False, f"Memory check failed: {e}", severity="warning"))

    return results


# ---------------------------------------------------------------------------
# 8. REVIEW tasks without PRs
# ---------------------------------------------------------------------------
async def check_review_without_prs() -> List[HealthCheckResult]:
    from agents.mission_control.core.pr_check import has_open_pr_for_task

    results = []

    async with AsyncSessionLocal() as session:
        review_tasks = await session.execute(
            select(Task).where(Task.status == TaskStatus.REVIEW)
        )
        tasks = review_tasks.scalars().all()

        if not tasks:
            results.append(HealthCheckResult("review_prs", True, "No tasks in REVIEW"))
            return results

        for task in tasks:
            # Get repo from mission_config (preferred) or description (fallback)
            config = task.mission_config or {}
            repo = config.get("repository")
            if not repo and task.description and "Repository:" in task.description:
                for line in task.description.split("\n"):
                    if line.startswith("Repository:"):
                        repo = line.split(":", 1)[1].strip()
                        break

            if not repo:
                continue

            task_id_short = str(task.id)[:8]
            has_pr, _ = await has_open_pr_for_task(repo, task_id_short)

            if not has_pr:
                task.status = TaskStatus.ASSIGNED
                session.add(Activity(
                    type=ActivityType.STATUS_CHANGE,
                    task_id=task.id,
                    message="Vision Healer: REVIEW task has no PR, reset to ASSIGNED",
                ))
                results.append(HealthCheckResult(
                    "review_prs", False,
                    f"Task '{task.title[:50]}' in REVIEW without PR",
                    fix_applied="Reset to ASSIGNED",
                    severity="warning",
                ))

        await session.commit()

    if not any(r.name == "review_prs" and not r.passed for r in results):
        results.append(HealthCheckResult("review_prs", True, "All REVIEW tasks have PRs"))

    return results


# ---------------------------------------------------------------------------
# 9. Long-running tasks — IN_PROGRESS >3h no commits, >6h absolute
# ---------------------------------------------------------------------------
async def check_long_running_tasks() -> List[HealthCheckResult]:
    results = []
    now = datetime.now(timezone.utc)
    hard_cap = timedelta(hours=settings.vision_task_hard_cap_hours)
    soft_cap = timedelta(hours=settings.vision_task_soft_cap_hours)

    async with AsyncSessionLocal() as session:
        in_progress = await session.execute(
            select(Task).where(Task.status == TaskStatus.IN_PROGRESS)
        )
        tasks = in_progress.scalars().all()

        if not tasks:
            results.append(HealthCheckResult("long_running", True, "No IN_PROGRESS tasks"))
            return results

        for task in tasks:
            age = now - task.updated_at.replace(
                tzinfo=timezone.utc if task.updated_at.tzinfo is None else task.updated_at.tzinfo
            )
            age_h = age.total_seconds() / 3600

            if age > hard_cap:
                # Hard cap: reset to INBOX for reassignment
                task.status = TaskStatus.INBOX
                session.add(Activity(
                    type=ActivityType.STATUS_CHANGE,
                    task_id=task.id,
                    message=f"Vision Healer: task IN_PROGRESS for {age_h:.1f}h (>6h hard cap), reset to INBOX",
                ))
                results.append(HealthCheckResult(
                    "long_running", False,
                    f"Task '{task.title[:50]}' running {age_h:.1f}h (hard cap 6h)",
                    fix_applied="Reset to INBOX for reassignment",
                    severity="critical",
                ))
            elif age > soft_cap:
                # Soft cap: warn but don't reset yet
                results.append(HealthCheckResult(
                    "long_running", False,
                    f"Task '{task.title[:50]}' running {age_h:.1f}h (>3h soft cap)",
                    severity="warning",
                ))

        await session.commit()

    if not any(r.name == "long_running" and not r.passed for r in results):
        results.append(HealthCheckResult("long_running", True, "No long-running tasks"))

    return results


# Patterns for files agents are ALLOWED to modify (working state, not code)
import re

_ALLOWED_PATTERNS = [
    re.compile(r"agents/squad/\w+/daily/"),       # daily work logs
    re.compile(r"agents/squad/\w+/WORKING\.md"),   # agent working state
    re.compile(r"logs/"),                          # log directory
]


def _is_allowed_change(filepath: str) -> bool:
    """Return True if this file is expected agent working state (not code)."""
    return any(p.search(filepath) for p in _ALLOWED_PATTERNS)


async def check_repo_clean() -> List[HealthCheckResult]:
    """Agents must NEVER modify mission-control source code. Revert unauthorized changes."""
    results = []
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    ))))

    try:
        status = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain", "--ignore-submodules"],
            capture_output=True, text=True, timeout=10,
        )
        dirty_lines = [l for l in status.stdout.strip().splitlines() if l.strip()]

        if not dirty_lines:
            results.append(HealthCheckResult(
                "repo_clean", True, "Repository is clean — no agent modifications"
            ))
            return results

        # Split into allowed working files vs unauthorized code changes
        unauthorized = []
        for line in dirty_lines:
            # git status --porcelain: first 2 chars are status, then space, then path
            filepath = line[3:].strip()
            if not _is_allowed_change(filepath):
                unauthorized.append(line)

        if not unauthorized:
            results.append(HealthCheckResult(
                "repo_clean", True,
                f"Only allowed working files modified ({len(dirty_lines)} files)"
            ))
            return results

        # We have unauthorized changes — revert them
        detail = f"{len(unauthorized)} unauthorized: {', '.join(l[3:].strip() for l in unauthorized[:8])}"
        logger.warning("repo_unauthorized_changes", files=[l[3:].strip() for l in unauthorized])

        modified = [l for l in unauthorized if not l.startswith("??")]
        untracked = [l for l in unauthorized if l.startswith("??")]

        if modified:
            # Extract just the file paths and checkout each
            paths = [l[3:].strip() for l in modified]
            subprocess.run(
                ["git", "-C", repo_root, "checkout", "--"] + paths,
                capture_output=True, timeout=10,
            )

        if untracked:
            paths = [l[3:].strip() for l in untracked]
            for p in paths:
                full = os.path.join(repo_root, p)
                if os.path.exists(full):
                    os.remove(full)

        # Verify
        verify = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain", "--ignore-submodules"],
            capture_output=True, text=True, timeout=10,
        )
        remaining = [
            l for l in verify.stdout.strip().splitlines()
            if l.strip() and not _is_allowed_change(l[3:].strip())
        ]

        if remaining:
            results.append(HealthCheckResult(
                "repo_clean", False,
                f"Repo still has unauthorized changes: {remaining[0][:80]}",
                severity="critical",
            ))
        else:
            results.append(HealthCheckResult(
                "repo_clean", False, detail,
                fix_applied=f"Reverted {len(modified)} modified, removed {len(untracked)} untracked",
            ))

    except Exception as e:
        results.append(HealthCheckResult(
            "repo_clean", False, f"Repo check failed: {e}", severity="critical"
        ))

    return results


# ---------------------------------------------------------------------------
# Master checklist — run all checks
# ---------------------------------------------------------------------------
ALL_CHECKS = [
    check_stale_tasks,
    check_zombie_processes,
    check_chatbot_health,
    check_service_health,
    check_inbox_with_assignees,
    check_log_bloat,
    check_memory_pressure,
    check_review_without_prs,
    check_long_running_tasks,
    check_repo_clean,
]


async def run_all_checks() -> List[HealthCheckResult]:
    """Run all health checks and return combined results."""
    all_results = []
    for check_fn in ALL_CHECKS:
        try:
            results = await check_fn()
            all_results.extend(results)
        except Exception as e:
            all_results.append(HealthCheckResult(
                check_fn.__name__, False,
                f"Check crashed: {e}",
                severity="critical",
            ))
    return all_results
