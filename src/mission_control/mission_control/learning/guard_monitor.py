"""
Guard Monitor — tracks guard blocks and surfaces repeated blocks as workflow patterns.

When 3+ blocks of the same transition occur within 24h, a workflow-type learning
pattern is created as a health alert.  Guards are never auto-modified; this is
purely observational.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

logger = structlog.get_logger()

# In-memory ring buffer: (mission_type, from_state, to_state, guard, timestamp)
_block_log: list[dict] = []
_MAX_LOG = 500
_BLOCK_THRESHOLD = 3
_WINDOW_HOURS = 24


async def record_guard_block(
    mission_type: str,
    from_state: str,
    to_state: str,
    guard_name: str,
    agent_name: Optional[str] = None,
    task_id: Optional[str] = None,
):
    """Record a guard block and check if threshold is reached."""
    now = datetime.now(timezone.utc)
    entry = {
        "mission_type": mission_type,
        "from_state": from_state,
        "to_state": to_state,
        "guard": guard_name,
        "agent": agent_name,
        "task_id": task_id,
        "ts": now,
    }
    _block_log.append(entry)
    if len(_block_log) > _MAX_LOG:
        _block_log[:] = _block_log[-_MAX_LOG:]

    # Check threshold
    cutoff = now - timedelta(hours=_WINDOW_HOURS)
    key = (mission_type, from_state, to_state, guard_name)
    recent = [
        b for b in _block_log
        if b["ts"] >= cutoff
        and (b["mission_type"], b["from_state"], b["to_state"], b["guard"]) == key
    ]

    if len(recent) >= _BLOCK_THRESHOLD:
        await _create_guard_alert_pattern(
            mission_type=mission_type,
            from_state=from_state,
            to_state=to_state,
            guard_name=guard_name,
            block_count=len(recent),
        )


async def _create_guard_alert_pattern(
    mission_type: str,
    from_state: str,
    to_state: str,
    guard_name: str,
    block_count: int,
):
    """Create or update a workflow-type learning pattern for repeated guard blocks."""
    try:
        from sqlalchemy import select

        from mission_control.mission_control.core.database import (
            AsyncSessionLocal,
            LearningPattern,
            LearningType,
        )

        trigger = (
            f"Guard '{guard_name}' blocked {from_state}→{to_state} "
            f"in {mission_type} mission {block_count} times in {_WINDOW_HOURS}h"
        )

        async with AsyncSessionLocal() as session:
            # Check for existing pattern with same trigger prefix
            prefix = f"Guard '{guard_name}' blocked {from_state}→{to_state} in {mission_type}"
            existing = (await session.execute(
                select(LearningPattern).where(
                    LearningPattern.trigger_text.startswith(prefix),
                    LearningPattern.mission_type == mission_type,
                )
            )).scalar_one_or_none()

            now = datetime.now(timezone.utc)
            if existing:
                existing.trigger_text = trigger
                existing.occurrence_count += 1
                existing.last_used = now
                # Boost confidence on repeated occurrence
                existing.confidence = min(1.0, existing.confidence + 0.05)
            else:
                session.add(LearningPattern(
                    type=LearningType.WORKFLOW,
                    trigger_text=trigger,
                    resolution={
                        "action": f"Investigate why {guard_name} repeatedly blocks "
                                  f"{from_state}→{to_state} in {mission_type} workflows",
                        "suggestion": "Check if guard condition is too strict or if "
                                      "upstream steps are failing silently",
                    },
                    confidence=0.6,
                    occurrence_count=block_count,
                    last_used=now,
                    mission_type=mission_type,
                ))
            await session.commit()

        logger.warning(
            "Guard block alert created",
            guard=guard_name,
            transition=f"{from_state}→{to_state}",
            mission=mission_type,
            count=block_count,
        )
    except Exception as e:
        logger.error("Failed to create guard alert pattern", error=str(e))


def get_recent_blocks(
    mission_type: Optional[str] = None,
    hours: int = 24,
) -> list[dict]:
    """Return recent guard blocks, optionally filtered by mission."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    blocks = [b for b in _block_log if b["ts"] >= cutoff]
    if mission_type:
        blocks = [b for b in blocks if b["mission_type"] == mission_type]
    return [
        {
            "mission_type": b["mission_type"],
            "transition": f"{b['from_state']}→{b['to_state']}",
            "guard": b["guard"],
            "agent": b["agent"],
            "timestamp": b["ts"].isoformat(),
        }
        for b in blocks
    ]
