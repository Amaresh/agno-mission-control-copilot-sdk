"""
Learning event → pattern aggregation processor.

Periodically processes unprocessed learning_events and aggregates them
into learning_patterns for use by agents at prompt-enrichment time.
"""

from collections import defaultdict
from typing import Optional

import structlog
from sqlalchemy import select, update

from mission_control.mission_control.core.database import (
    AsyncSessionLocal,
    LearningEvent,
    LearningPattern,
    LearningType,
)

logger = structlog.get_logger()

BATCH_SIZE = 500


async def process_learning_events() -> dict:
    """
    Process unprocessed learning events into aggregated patterns.

    Returns a summary dict with counts of events processed and patterns created/updated.
    """
    async with AsyncSessionLocal() as session:
        stmt = (
            select(LearningEvent)
            .where(LearningEvent.processed == False)  # noqa: E712
            .order_by(LearningEvent.created_at)
            .limit(BATCH_SIZE)
        )
        result = await session.execute(stmt)
        events = list(result.scalars().all())

        if not events:
            logger.info("No unprocessed learning events")
            return {"events_processed": 0, "patterns_created": 0, "patterns_updated": 0}

        task_outcomes = [e for e in events if e.event_type == "task_outcome"]
        tool_usages = [e for e in events if e.event_type == "tool_usage"]
        errors = [e for e in events if e.event_type == "error"]
        heartbeats = [e for e in events if e.event_type == "heartbeat"]

        created = 0
        updated = 0

        c, u = await _process_task_outcomes(session, task_outcomes)
        created += c
        updated += u

        c, u = await _process_tool_usage(session, tool_usages)
        created += c
        updated += u

        c, u = await _process_errors(session, errors)
        created += c
        updated += u

        await _mark_heartbeats_processed(session, heartbeats)

        event_ids = [e.id for e in events]
        await session.execute(
            update(LearningEvent)
            .where(LearningEvent.id.in_(event_ids))
            .values(processed=True)
        )

        await session.commit()

        summary = {
            "events_processed": len(events),
            "patterns_created": created,
            "patterns_updated": updated,
        }
        logger.info("Learning aggregation complete", **summary)
        return summary


async def _mark_heartbeats_processed(session, events):
    """Mark heartbeat events as processed without creating patterns."""
    if not events:
        return
    try:
        ids = [e.id for e in events]
        await session.execute(
            update(LearningEvent)
            .where(LearningEvent.id.in_(ids))
            .values(processed=True)
        )
    except Exception as e:
        logger.error("Failed to mark heartbeats processed", error=str(e))


async def _process_task_outcomes(session, events) -> tuple[int, int]:
    """
    Aggregate task outcomes into workflow patterns.

    Groups by agent_name and creates patterns like:
      trigger: "Agent X task execution"
    """
    if not events:
        return 0, 0

    created, updated = 0, 0

    by_agent = defaultdict(list)
    for e in events:
        agent = (e.context or {}).get("agent_name", "unknown")
        by_agent[agent].append(e)

    for agent_name, agent_events in by_agent.items():
        successes = sum(1 for e in agent_events if (e.context or {}).get("success", False))
        total = len(agent_events)
        success_rate = successes / total if total > 0 else 0.0

        trigger = f"{agent_name} task execution"
        pattern = await _find_pattern(session, LearningType.WORKFLOW, trigger)

        if success_rate < 0.5:
            action = (
                f"{agent_name} has low success rate ({success_rate:.0%}). "
                f"Consider reviewing task complexity or agent instructions."
            )
        elif success_rate >= 0.8:
            action = f"{agent_name} is reliable for task execution ({success_rate:.0%})."
        else:
            action = (
                f"{agent_name} has moderate success rate ({success_rate:.0%}). "
                f"Monitor task outcomes."
            )

        resolution = {"avg_success_rate": round(success_rate, 3), "action": action}

        if pattern:
            pattern.occurrence_count += total
            pattern.confidence = round((pattern.confidence + success_rate) / 2, 3)
            pattern.resolution = resolution
            updated += 1
        else:
            trigger_full = f"{agent_name} task execution (success rate: {success_rate:.0%})"
            pattern = LearningPattern(
                type=LearningType.WORKFLOW,
                trigger_text=trigger_full,
                context={"agent_name": agent_name, "total_tasks": total},
                resolution=resolution,
                confidence=round(success_rate, 3),
                occurrence_count=total,
            )
            session.add(pattern)
            created += 1

    return created, updated


async def _process_tool_usage(session, events) -> tuple[int, int]:
    """
    Aggregate tool usage events into tool_usage patterns.

    Groups by tool_name and tracks success/failure rates.
    """
    if not events:
        return 0, 0

    created, updated = 0, 0

    by_tool = defaultdict(list)
    for e in events:
        tool = (e.context or {}).get("tool_name", "unknown")
        by_tool[tool].append(e)

    for tool_name, tool_events in by_tool.items():
        successes = sum(1 for e in tool_events if (e.context or {}).get("success", False))
        total = len(tool_events)
        success_rate = successes / total if total > 0 else 0.0

        trigger = f"'{tool_name}' usage pattern"
        pattern = await _find_pattern(session, LearningType.TOOL_USAGE, trigger)

        if success_rate < 0.5:
            action = (
                f"'{tool_name}' has a low success rate ({success_rate:.0%}). "
                f"Check tool arguments and prerequisites."
            )
        elif success_rate >= 0.8:
            action = f"'{tool_name}' is reliable ({total} uses). Use confidently."
        else:
            action = (
                f"'{tool_name}' has moderate reliability ({success_rate:.0%} success). "
                f"Verify results after use."
            )

        resolution = {"success_rate": round(success_rate, 3), "action": action}

        if pattern:
            pattern.occurrence_count += total
            pattern.confidence = round((pattern.confidence + success_rate) / 2, 3)
            pattern.resolution = resolution
            updated += 1
        else:
            pattern = LearningPattern(
                type=LearningType.TOOL_USAGE,
                trigger_text=trigger,
                context={"tool_name": tool_name, "total_uses": total},
                resolution=resolution,
                confidence=round(success_rate, 3),
                occurrence_count=total,
            )
            session.add(pattern)
            created += 1

    return created, updated


async def _process_errors(session, events) -> tuple[int, int]:
    """Aggregate error events into error_fix patterns."""
    if not events:
        return 0, 0

    created, updated = 0, 0

    by_error = defaultdict(list)
    for e in events:
        msg = (e.context or {}).get("error_message", "unknown error")
        key = msg[:80]
        by_error[key].append(e)

    for error_key, error_events in by_error.items():
        total = len(error_events)
        trigger = error_key
        pattern = await _find_pattern(session, LearningType.ERROR_FIX, trigger)

        action = f"{error_key} error. Review agent instructions or MCP tool configuration."
        resolution = {"action": action}

        if pattern:
            pattern.occurrence_count += total
            pattern.resolution = resolution
            updated += 1
        else:
            pattern = LearningPattern(
                type=LearningType.ERROR_FIX,
                trigger_text=trigger,
                context={"error_sample": error_key, "occurrences": total},
                resolution=resolution,
                confidence=0.5,
                occurrence_count=total,
            )
            session.add(pattern)
            created += 1

    return created, updated


async def _find_pattern(
    session, pattern_type: LearningType, trigger_contains: str
) -> Optional[LearningPattern]:
    """Find an existing pattern by type and trigger text prefix."""
    stmt = (
        select(LearningPattern)
        .where(LearningPattern.type == pattern_type)
        .where(LearningPattern.trigger_text.ilike(f"%{trigger_contains}%"))
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
