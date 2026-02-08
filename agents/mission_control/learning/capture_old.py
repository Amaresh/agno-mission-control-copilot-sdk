"""
Learning capture module for agent event tracking.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import select

from agents.mission_control.core.database import (
    AsyncSessionLocal,
    LearningEvent,
    LearningPattern,
    LearningType,
)

logger = structlog.get_logger()


async def capture_learning_event(
    agent_id: str,
    event_type: str,
    context: dict[str, Any],
    outcome: Optional[dict[str, Any]] = None,
) -> uuid.UUID:
    """
    Capture a learning event for later processing.

    Events are stored and periodically aggregated into patterns.
    """
    async with AsyncSessionLocal() as session:
        event = LearningEvent(
            agent_id=None,  # TODO: Resolve agent UUID from session_key
            event_type=event_type,
            context=context,
            outcome=outcome or {},
            processed=False,
        )
        session.add(event)
        await session.commit()

        logger.info(
            "Captured learning event",
            event_id=str(event.id),
            event_type=event_type,
            agent=agent_id,
        )

        return event.id


async def capture_error_fix(
    trigger: str,
    error_context: dict[str, Any],
    resolution: dict[str, Any],
    agent_id: Optional[str] = None,
):
    """
    Capture an error→fix pattern directly.

    Used when an agent successfully recovers from an error.
    """
    async with AsyncSessionLocal() as session:
        # Check if similar pattern exists
        # TODO: Use vector similarity search

        pattern = LearningPattern(
            type=LearningType.ERROR_FIX,
            trigger_text=trigger,
            context=error_context,
            resolution=resolution,
            confidence=0.6,  # Initial confidence
            occurrence_count=1,
        )
        session.add(pattern)
        await session.commit()

        logger.info(
            "Captured error→fix pattern",
            pattern_id=str(pattern.id),
            trigger=trigger[:100],
        )


async def capture_tool_usage(
    intent: str,
    tool_name: str,
    tool_args: dict[str, Any],
    success: bool,
    agent_id: Optional[str] = None,
):
    """
    Capture a tool usage pattern.

    Helps the system learn which tools work best for which intents.
    """
    await capture_learning_event(
        agent_id=agent_id or "unknown",
        event_type="tool_usage",
        context={
            "intent": intent,
            "tool_name": tool_name,
            "tool_args": tool_args,
        },
        outcome={
            "success": success,
        },
    )


async def get_relevant_patterns(
    query: str,
    pattern_type: Optional[LearningType] = None,
    limit: int = 5,
) -> list[LearningPattern]:
    """
    Get relevant learning patterns for a query.

    TODO: Implement vector similarity search with pgvector.
    For now, uses simple text matching.
    """
    async with AsyncSessionLocal() as session:
        stmt = select(LearningPattern).order_by(
            LearningPattern.confidence.desc(),
            LearningPattern.occurrence_count.desc(),
        ).limit(limit)

        if pattern_type:
            stmt = stmt.where(LearningPattern.type == pattern_type)

        result = await session.execute(stmt)
        patterns = result.scalars().all()

        return list(patterns)


async def update_pattern_usage(pattern_id: uuid.UUID, success: bool):
    """
    Update a pattern's usage stats after it was applied.
    """
    async with AsyncSessionLocal() as session:
        stmt = select(LearningPattern).where(LearningPattern.id == pattern_id)
        result = await session.execute(stmt)
        pattern = result.scalar_one_or_none()

        if pattern:
            pattern.occurrence_count += 1
            pattern.last_used = datetime.now(timezone.utc)

            # Adjust confidence based on success
            if success:
                pattern.confidence = min(1.0, pattern.confidence + 0.05)
            else:
                pattern.confidence = max(0.1, pattern.confidence - 0.1)

            await session.commit()

            logger.info(
                "Updated pattern usage",
                pattern_id=str(pattern_id),
                success=success,
                new_confidence=pattern.confidence,
            )
