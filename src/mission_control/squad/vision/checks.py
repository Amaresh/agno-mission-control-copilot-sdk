"""
Health check functions for Vision Healer agent.

Each check returns a HealthCheckResult. Checks are deterministic —
no LLM calls. They query the DB, shell, and GitHub API directly.
"""

import asyncio
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx
import structlog
from sqlalchemy import select, text

from mission_control.config import settings
from mission_control.mission_control.core.database import (
    Activity,
    ActivityType,
    AsyncSessionLocal,
    Task,
    TaskAssignment,
    TaskStatus,
)
from mission_control.mission_control.core.database import (
    Agent as AgentModel,
)

logger = structlog.get_logger()


async def _diagnose_empty_branch(repo: str, agent_branch: str, assumed_base: str = "main") -> Optional[str]:
    """Check if agent branch is 0 ahead of assumed base; if so, find the real base.

    Returns the correct base branch name, or None if no fix needed.
    """
    token = settings.github_token
    if not token:
        return None
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Check if the agent branch exists
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/branches/{agent_branch}",
                headers=headers,
            )
            if resp.status_code == 404:
                return None  # no branch yet — not our problem

            # Compare: how many commits is agent branch ahead of assumed base?
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/compare/{assumed_base}...{agent_branch}",
                headers=headers,
            )
            if resp.status_code != 200:
                return None
            compare = resp.json()
            if compare.get("ahead_by", 1) > 0:
                return None  # branch has real commits — not an empty-branch problem

            # Branch is 0 ahead of base → find the real base branch
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/branches",
                headers=headers, params={"per_page": 30},
            )
            if resp.status_code != 200:
                return None
            branches = resp.json()

            best_branch = None
            best_ahead = 0
            for b in branches:
                bname = b["name"]
                if bname == assumed_base or bname == agent_branch:
                    continue
                # How many commits ahead of assumed_base is this branch?
                cmp = await client.get(
                    f"https://api.github.com/repos/{repo}/compare/{assumed_base}...{bname}",
                    headers=headers,
                )
                if cmp.status_code == 200:
                    ahead = cmp.json().get("ahead_by", 0)
                    if ahead > best_ahead:
                        best_ahead = ahead
                        best_branch = bname

            if best_branch and best_ahead > 0:
                logger.info("Diagnosed empty branch", repo=repo,
                            agent_branch=agent_branch, wrong_base=assumed_base,
                            correct_base=best_branch, ahead_by=best_ahead)
                return best_branch
    except Exception as e:
        logger.error("Empty branch diagnosis failed", error=str(e))
    return None


async def _fix_empty_branch(repo: str, agent_branch: str, correct_base: str) -> bool:
    """Delete the empty agent branch so it can be recreated from the correct base."""
    token = settings.github_token
    if not token:
        return False
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"https://api.github.com/repos/{repo}/git/refs/heads/{agent_branch}",
                headers=headers,
            )
            return resp.status_code in (200, 204)
    except Exception as e:
        logger.error("Failed to delete empty branch", error=str(e))
        return False


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
    from mission_control.mission_control.core.pr_check import has_open_pr_for_task

    results = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1.5)

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
            old_status = task.status

            # Check if a PR already exists — promote to REVIEW instead of resetting
            repo = None
            if task.description and "Repository:" in task.description:
                for line in task.description.split("\n"):
                    if line.strip().startswith("Repository:"):
                        repo = line.split(":", 1)[1].strip()
                        break

            if repo:
                task_id_short = str(task.id)[:8]
                has_pr, pr_url = await has_open_pr_for_task(repo, task_id_short)
                if has_pr:
                    task.status = TaskStatus.REVIEW
                    session.add(Activity(
                        type=ActivityType.TASK_STATUS_CHANGED,
                        task_id=task.id,
                        message=f"Vision Healer: stale {old_status.value} task ({age_h:.1f}h) has PR ({pr_url}), promoted to REVIEW",
                    ))
                    results.append(HealthCheckResult(
                        "stale_tasks", False,
                        f"Task '{task.title[:50]}' stale ({age_h:.1f}h) but has PR",
                        fix_applied=f"Promoted to REVIEW ({pr_url})",
                        severity="info",
                    ))
                    continue

            # No PR found — check if this is an empty-branch loop
            if repo:
                task_id_short = str(task.id)[:8]
                # Find which agent owns this task to derive branch name
                assignments = await session.execute(
                    select(TaskAssignment.agent_id).where(TaskAssignment.task_id == task.id)
                )
                agent_ids = [a for (a,) in assignments.all()]
                agent_name = None
                if agent_ids:
                    agent_rec = await session.execute(
                        select(AgentModel.name).where(AgentModel.id == agent_ids[0])
                    )
                    agent_name = agent_rec.scalar_one_or_none()

                if agent_name:
                    agent_branch = f"{agent_name.lower()}/{task_id_short}"
                    # Parse current base branch from description (default "main")
                    current_base = "main"
                    if task.description and "Base Branch:" in task.description:
                        for dline in task.description.split("\n"):
                            if dline.strip().startswith("Base Branch:"):
                                current_base = dline.split(":", 1)[1].strip()
                                break

                    correct_base = await _diagnose_empty_branch(repo, agent_branch, current_base)
                    if correct_base:
                        # Fix: delete empty branch, inject Base Branch into description, reset
                        deleted = await _fix_empty_branch(repo, agent_branch, correct_base)
                        if task.description and "Base Branch:" in task.description:
                            task.description = task.description.replace(
                                f"Base Branch: {current_base}", f"Base Branch: {correct_base}"
                            )
                        else:
                            task.description = (task.description or "") + f"\nBase Branch: {correct_base}"
                        task.status = TaskStatus.INBOX
                        fix_msg = (
                            f"Vision Healer: stale {old_status.value} task ({age_h:.1f}h) — "
                            f"empty branch detected (0 ahead of '{current_base}'). "
                            f"Correct base is '{correct_base}'. "
                            f"{'Deleted' if deleted else 'Failed to delete'} empty branch '{agent_branch}', "
                            f"injected Base Branch into description, reset to INBOX."
                        )
                        session.add(Activity(
                            type=ActivityType.TASK_STATUS_CHANGED,
                            task_id=task.id,
                            message=fix_msg,
                        ))
                        results.append(HealthCheckResult(
                            "stale_tasks", False,
                            f"Task '{task.title[:50]}' stuck on empty branch",
                            fix_applied=f"Fixed base branch: {current_base} → {correct_base}",
                            severity="warning",
                        ))
                        continue

            # Fallback: no repo or no empty-branch issue — just reset to INBOX
            task.status = TaskStatus.INBOX
            session.add(Activity(
                type=ActivityType.TASK_STATUS_CHANGED,
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

    # ── 1. Service liveness ──────────────────────────────────────────────
    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "is-active", "mc-bot"],
            text=True, timeout=5,
        ).strip()
        bot_active = out == "active"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        bot_active = False

    if not bot_active:
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

    # ── 2. Response-time analysis (since last Vision heartbeat) ──────────
    SLOW_THRESHOLD_MS = 300_000  # 300 s

    async with AsyncSessionLocal() as session:
        # Find Vision's last heartbeat to scope the window
        vision_row = await session.execute(
            select(AgentModel.last_heartbeat).where(
                AgentModel.name.ilike("vision")
            )
        )
        vision_hb = (vision_row.scalar_one_or_none() or
                     datetime.now(timezone.utc) - timedelta(hours=2))
        if vision_hb.tzinfo is None:
            vision_hb = vision_hb.replace(tzinfo=timezone.utc)

        # Completed responses since last run
        responded = await session.execute(text("""
            SELECT extra_data FROM activities
            WHERE type = 'MESSAGE_RESPONDED'
              AND created_at > :since
            ORDER BY created_at
        """), {"since": vision_hb})
        latencies = []
        for row in responded:
            ed = row.extra_data or {}
            ms = ed.get("response_time_ms")
            if ms is not None:
                latencies.append(int(ms))

        if latencies:
            latencies.sort()
            median = latencies[len(latencies) // 2]
            p95 = latencies[int(len(latencies) * 0.95)]
            max_ms = latencies[-1]
            slow_count = sum(1 for l in latencies if l > SLOW_THRESHOLD_MS)

            if slow_count:
                results.append(HealthCheckResult(
                    "chatbot_latency", False,
                    f"{slow_count}/{len(latencies)} responses exceeded {SLOW_THRESHOLD_MS // 1000}s "
                    f"(median={median // 1000}s, p95={p95 // 1000}s, max={max_ms // 1000}s)",
                    severity="warning",
                ))
            else:
                results.append(HealthCheckResult(
                    "chatbot_latency", True,
                    f"{len(latencies)} responses OK "
                    f"(median={median // 1000}s, max={max_ms // 1000}s)"))
        else:
            results.append(HealthCheckResult(
                "chatbot_latency", True, "No chatbot responses since last check"))

        # ── 3. Stuck requests — received but no response yet ─────────────
        stuck_rows = await session.execute(text("""
            SELECT r.id, r.extra_data, r.created_at
            FROM activities r
            WHERE r.type = 'MESSAGE_RECEIVED'
              AND r.created_at > :since
              AND NOT EXISTS (
                  SELECT 1 FROM activities resp
                  WHERE resp.type = 'MESSAGE_RESPONDED'
                    AND resp.extra_data->>'request_activity_id' = r.id::text
              )
            ORDER BY r.created_at
        """), {"since": vision_hb})
        now = datetime.now(timezone.utc)

        for row in stuck_rows:
            created = row.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_s = (now - created).total_seconds()
            if age_s < SLOW_THRESHOLD_MS / 1000:
                continue  # still within acceptable window

            ed = row.extra_data or {}
            msg_text = ed.get("message_text", "")
            chat_id = ed.get("chat_id")

            # ── Heal: run the user's request via Jarvis agent directly ──
            fix_applied = None
            if msg_text:
                agent_result = await _run_stuck_request_via_agent(msg_text)
                if agent_result:
                    from mission_control.squad.vision.notify import send_telegram
                    if chat_id:
                        await send_telegram(
                            f"Your earlier request timed out, so Vision handled it directly:\n\n"
                            f"{agent_result[:3500]}"
                        )
                    fix_applied = f"Fulfilled via Jarvis agent ({len(agent_result)} chars)"

                    # Mark the original request as responded (prevents re-processing)
                    session.add(Activity(
                        type=ActivityType.MESSAGE_RESPONDED,
                        message=f"Vision fulfilled stuck request via agent ({int(age_s)}s late)",
                        extra_data={
                            "request_activity_id": str(row.id),
                            "response_time_ms": int(age_s * 1000),
                            "response_len": len(agent_result),
                            "chat_id": chat_id,
                            "fulfilled_by": "vision",
                        },
                    ))

            if not fix_applied:
                # CLI also failed — notify human with the issue
                from mission_control.squad.vision.notify import send_telegram
                if chat_id:
                    await send_telegram(
                        f"Your request could not be completed automatically:\n\n"
                        f"> {msg_text[:500]}\n\n"
                        f"Vision tried to handle it but also failed. "
                        f"Please retry or rephrase your request."
                    )
                fix_applied = "Notified user of failure"

            results.append(HealthCheckResult(
                "chatbot_stuck", False,
                f"Request stuck for {int(age_s)}s: {msg_text[:60]}",
                fix_applied=fix_applied,
                severity="warning",
            ))

        await session.commit()

    if not any(r.name == "chatbot_stuck" for r in results):
        results.append(HealthCheckResult(
            "chatbot_stuck", True, "No stuck chatbot requests"))

    return results


async def _run_stuck_request_via_agent(user_message: str) -> Optional[str]:
    """
    Run a stuck chatbot request via Copilot CLI connected to the MC MCP server.

    This avoids re-using the Jarvis agent pipeline (which is what timed out)
    while still giving the LLM full access to Mission Control tools via MCP.
    """
    mcp_config = {
        "mcpServers": {
            "mission-control": {
                "type": "sse",
                "url": "http://localhost:8001/sse"
            }
        }
    }
    import json
    mcp_json = json.dumps(mcp_config)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "copilot", "-p", user_message,
            "--additional-mcp-config", mcp_json,
            "--allow-all",
            cwd=os.path.expanduser("~/projects/mission-control"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=120,
        )
        output = stdout.decode().strip() if stdout else ""
        if proc.returncode == 0 and output:
            logger.info("Vision fulfilled stuck request via CLI+MCP", output_len=len(output))
            return output
        else:
            err = stderr.decode() if stderr else ""
            logger.error("Vision CLI+MCP failed", returncode=proc.returncode, stderr=err[:300])
            return None
    except asyncio.TimeoutError:
        logger.error("Vision CLI+MCP timed out — killing subprocess")
        if proc:
            proc.kill()
            await proc.wait()
        return None
    except Exception as e:
        logger.error("Vision CLI+MCP error", error=str(e))
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        return None


# ---------------------------------------------------------------------------
# 4. Service health — all 4 systemd services running
# ---------------------------------------------------------------------------
async def check_service_health() -> List[HealthCheckResult]:
    results = []
    services = ["mc-mcp", "mc-api", "mc-bot", "mc-scheduler"]

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
    threshold = 50 * 1024 * 1024  # 50MB

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
        results.append(HealthCheckResult("log_bloat", True, "All logs under 50MB"))

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

        if used_pct > 90:
            results.append(HealthCheckResult(
                "memory", False,
                f"RAM: {used_pct:.0f}% used ({available_mb}MB available)",
                severity="critical",
            ))
        else:
            results.append(HealthCheckResult(
                "memory", True, f"RAM: {used_pct:.0f}% ({available_mb}MB available)"))

        if swap_pct > 80:
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
    from mission_control.mission_control.core.pr_check import has_open_pr_for_task

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
            # Review tasks don't produce their own PRs — skip
            if task.mission_type == "review":
                continue

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
                    type=ActivityType.TASK_STATUS_CHANGED,
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
    hard_cap = timedelta(hours=6)
    soft_cap = timedelta(hours=3)

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
                    type=ActivityType.TASK_STATUS_CHANGED,
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
# Import temporary review cycle (runs last — includes a 5-min wait)
from mission_control.squad.vision.review_cycle import check_code_reviews

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
    check_code_reviews,       # TEMPORARY — remove after initial review complete
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
