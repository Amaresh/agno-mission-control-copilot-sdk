"""
Learning capture module for Mission Control.

Captures agent execution events (heartbeats, task outcomes, tool usage,
errors) into the learning_events table. Patterns are aggregated from
events and used to improve agent decision-making over time.

All capture functions are fire-and-forget safe — they catch exceptions
internally so they never crash the calling agent.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.mission_control.core.database import (
    AsyncSessionLocal,
    Agent as AgentModel,
    LearningEvent,
    LearningPattern,
    LearningType,
)

logger = structlog.get_logger()


# ============================================================
# Helpers
# ============================================================

async def resolve_agent_id(agent_name: str) -> Optional[uuid.UUID]:
    """Resolve agent UUID from name. Returns None if not found."""
    async with AsyncSessionLocal() as session:
        stmt = select(AgentModel.id).where(
            AgentModel.name.ilike(agent_name)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


# ============================================================
# Core capture
# ============================================================

async def capture_learning_event(
    agent_name: str,
    event_type: str,
    context: dict[str, Any],
    outcome: Optional[dict[str, Any]] = None,
) -> uuid.UUID:
    """
    Capture a raw learning event.

    Resolves agent_name → UUID automatically. If agent not found,
    stores event with agent_id=None (graceful degradation).
    """
    try:
        agent_id = await resolve_agent_id(agent_name)

        async with AsyncSessionLocal() as session:
            event = LearningEvent(
                agent_id=agent_id,
                event_type=event_type,
                context=context,
                outcome=outcome or {},
                processed=False,
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)

            logger.debug(
                "Captured learning event",
                event_id=str(event.id),
                event_type=event_type,
                agent=agent_name,
            )
            return event.id
    except Exception as e:
        logger.error("Failed to capture learning event", error=str(e), agent=agent_name)
        return uuid.uuid4()  # return a dummy ID so callers don't break


# ============================================================
# Specialized capture functions
# ============================================================

async def capture_heartbeat(
    agent_name: str,
    found_work: bool,
    work_type: Optional[str],
    duration_seconds: float,
) -> uuid.UUID:
    """Capture a heartbeat execution event."""
    return await capture_learning_event(
        agent_name=agent_name,
        event_type="heartbeat",
        context={
            "agent_name": agent_name,
            "found_work": found_work,
            "work_type": work_type,
        },
        outcome={
            "duration_seconds": duration_seconds,
        },
    )


async def capture_task_outcome(
    agent_name: str,
    task_id: str,
    task_title: str,
    from_status: str,
    to_status: str,
    duration_seconds: float,
    success: bool,
    response_preview: Optional[str] = None,
    error: Optional[str] = None,
) -> uuid.UUID:
    """Capture a task completion/failure event."""
    outcome = {
        "success": success,
        "duration_seconds": duration_seconds,
        "to_status": to_status,
    }
    if response_preview:
        outcome["response_preview"] = response_preview[:500]
    if error:
        outcome["error"] = error

    return await capture_learning_event(
        agent_name=agent_name,
        event_type="task_outcome",
        context={
            "agent_name": agent_name,
            "task_id": task_id,
            "task_title": task_title,
            "from_status": from_status,
        },
        outcome=outcome,
    )


async def capture_tool_usage(
    agent_name: str,
    tool_name: str,
    tool_args: dict[str, Any],
    success: bool,
    duration_seconds: float = 0.0,
    error: Optional[str] = None,
) -> uuid.UUID:
    """Capture an MCP tool invocation event."""
    outcome = {
        "success": success,
        "duration_seconds": duration_seconds,
    }
    if error:
        outcome["error"] = error

    return await capture_learning_event(
        agent_name=agent_name,
        event_type="tool_usage",
        context={
            "agent_name": agent_name,
            "tool_name": tool_name,
            "tool_args": _sanitize_args(tool_args),
        },
        outcome=outcome,
    )


async def capture_error_fix(
    trigger: str,
    error_context: dict[str, Any],
    resolution: dict[str, Any],
    agent_name: Optional[str] = None,
):
    """
    Capture an error→fix pattern directly.

    Used when an agent successfully recovers from an error.
    """
    try:
        async with AsyncSessionLocal() as session:
            pattern = LearningPattern(
                type=LearningType.ERROR_FIX,
                trigger_text=trigger,
                context=error_context,
                resolution=resolution,
                confidence=0.6,
                occurrence_count=1,
            )
            session.add(pattern)
            await session.commit()

            logger.info(
                "Captured error→fix pattern",
                pattern_id=str(pattern.id),
                trigger=trigger[:100],
            )
    except Exception as e:
        logger.error("Failed to capture error fix", error=str(e))


# ============================================================
# Pattern queries
# ============================================================

async def get_relevant_patterns(
    query: str,
    pattern_type: Optional[LearningType] = None,
    limit: int = 5,
) -> list[LearningPattern]:
    """
    Get relevant learning patterns for a query.

    Uses keyword matching on trigger_text: patterns that share words with
    the query are ranked higher.  Falls back to top patterns by confidence
    when no keyword overlap is found.

    TODO: Implement vector similarity search with pgvector for richer recall.
    """
    from sqlalchemy import case, func, literal

    async with AsyncSessionLocal() as session:
        # Extract meaningful keywords (>= 3 chars, lowercased)
        keywords = [w.lower() for w in query.split() if len(w) >= 3]

        if keywords:
            # Score each pattern by how many keywords appear in trigger_text
            keyword_hits = sum(
                case(
                    (func.lower(LearningPattern.trigger_text).contains(kw), 1),
                    else_=0,
                )
                for kw in keywords
            )
            stmt = (
                select(LearningPattern)
                .order_by(
                    keyword_hits.desc(),
                    LearningPattern.confidence.desc(),
                    LearningPattern.occurrence_count.desc(),
                )
                .limit(limit)
            )
        else:
            stmt = (
                select(LearningPattern)
                .order_by(
                    LearningPattern.confidence.desc(),
                    LearningPattern.occurrence_count.desc(),
                )
                .limit(limit)
            )

        if pattern_type:
            stmt = stmt.where(LearningPattern.type == pattern_type)

        # Only return patterns above a minimum confidence threshold
        stmt = stmt.where(LearningPattern.confidence >= 0.3)

        result = await session.execute(stmt)
        return list(result.scalars().all())


async def update_pattern_usage(pattern_id: uuid.UUID, success: bool):
    """Update a pattern's usage stats after it was applied."""
    try:
        async with AsyncSessionLocal() as session:
            stmt = select(LearningPattern).where(LearningPattern.id == pattern_id)
            result = await session.execute(stmt)
            pattern = result.scalar_one_or_none()

            if pattern:
                pattern.occurrence_count += 1
                pattern.last_used = datetime.now(timezone.utc)

                if success:
                    pattern.confidence = min(1.0, pattern.confidence + 0.05)
                else:
                    pattern.confidence = max(0.1, pattern.confidence - 0.1)

                await session.commit()

                logger.debug(
                    "Updated pattern usage",
                    pattern_id=str(pattern_id),
                    success=success,
                    new_confidence=pattern.confidence,
                )
    except Exception as e:
        logger.error("Failed to update pattern", error=str(e))


# ============================================================
# Helpers
# ============================================================

def _sanitize_args(args: dict[str, Any]) -> dict[str, Any]:
    """Sanitize tool args for storage — truncate large values."""
    sanitized = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            sanitized[k] = v[:200] + "..."
        else:
            sanitized[k] = v
    return sanitized


def format_patterns_for_context(patterns: list[LearningPattern]) -> str:
    """Format learning patterns into a context block for LLM injection.

    Returns a concise block that the LLM can use to avoid past mistakes
    and reuse successful strategies.  Returns empty string if no patterns.
    """
    if not patterns:
        return ""

    lines = [
        "<learned_patterns>",
        "The following patterns were learned from previous executions. "
        "Use them to inform your decisions:",
        "",
    ]

    for i, p in enumerate(patterns, 1):
        confidence_pct = int(p.confidence * 100)
        lines.append(f"{i}. [{p.type.value}] (confidence {confidence_pct}%) {p.trigger_text}")

        # Include resolution hint if available
        resolution = p.resolution or {}
        if isinstance(resolution, dict):
            action = resolution.get("action") or resolution.get("fix") or resolution.get("suggestion")
            if action:
                lines.append(f"   → {action}")
        elif isinstance(resolution, str):
            lines.append(f"   → {resolution}")

        lines.append("")

    lines.append("</learned_patterns>")
    return "\n".join(lines)
