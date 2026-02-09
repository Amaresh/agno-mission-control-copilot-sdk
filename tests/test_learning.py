"""Tests for the learning capture system.

Covers:
- Event capture (heartbeat, task outcome, error, tool usage)
- Pattern creation and confidence adjustment
- Agent ID resolution from name
- Integration: multiple events accumulate
"""

import uuid

import pytest
from sqlalchemy import select, func

from mission_control.mission_control.core.database import (
    AsyncSessionLocal,
    LearningEvent,
    LearningPattern,
    LearningType,
)
from mission_control.mission_control.learning.capture import (
    capture_learning_event,
    capture_error_fix,
    capture_tool_usage,
    capture_heartbeat,
    capture_task_outcome,
    get_relevant_patterns,
    update_pattern_usage,
    resolve_agent_id,
)
from tests.conftest import (
    create_test_agent,
    create_test_task,
    cleanup_test_agent,
    cleanup_test_task,
    cleanup_learning_events,
    cleanup_pattern,
)


# ============================================================
# resolve_agent_id
# ============================================================

class TestResolveAgentId:

    async def test_resolve_existing_agent(self):
        agent = await create_test_agent()
        try:
            agent_id = await resolve_agent_id(agent.name)
            assert agent_id == agent.id
        finally:
            await cleanup_test_agent(agent.id)

    async def test_resolve_nonexistent_returns_none(self):
        agent_id = await resolve_agent_id("NonExistentAgent999")
        assert agent_id is None

    async def test_resolve_case_insensitive(self):
        agent = await create_test_agent()
        try:
            agent_id = await resolve_agent_id(agent.name.upper())
            assert agent_id == agent.id
        finally:
            await cleanup_test_agent(agent.id)


# ============================================================
# capture_learning_event
# ============================================================

class TestCaptureLearningEvent:

    async def test_captures_event_with_agent_name(self):
        agent = await create_test_agent()
        try:
            event_id = await capture_learning_event(
                agent_name=agent.name,
                event_type="test_basic",
                context={"message": "hello"},
                outcome={"status": "ok"},
            )
            async with AsyncSessionLocal() as s:
                event = (await s.execute(
                    select(LearningEvent).where(LearningEvent.id == event_id)
                )).scalar_one()
                assert event.agent_id == agent.id
                assert event.event_type == "test_basic"
                assert event.context["message"] == "hello"
                assert event.outcome["status"] == "ok"
                assert event.processed is False
        finally:
            await cleanup_learning_events([event_id])
            await cleanup_test_agent(agent.id)

    async def test_captures_event_with_unknown_agent(self):
        event_id = await capture_learning_event(
            agent_name="UnknownAgent",
            event_type="test_unknown",
            context={"test": True},
        )
        try:
            async with AsyncSessionLocal() as s:
                event = (await s.execute(
                    select(LearningEvent).where(LearningEvent.id == event_id)
                )).scalar_one()
                assert event.agent_id is None
        finally:
            await cleanup_learning_events([event_id])


# ============================================================
# capture_heartbeat
# ============================================================

class TestCaptureHeartbeat:

    async def test_heartbeat_with_work(self):
        agent = await create_test_agent()
        try:
            event_id = await capture_heartbeat(
                agent_name=agent.name,
                found_work=True,
                work_type="task",
                duration_seconds=12.5,
            )
            async with AsyncSessionLocal() as s:
                event = (await s.execute(
                    select(LearningEvent).where(LearningEvent.id == event_id)
                )).scalar_one()
                assert event.event_type == "heartbeat"
                assert event.context["found_work"] is True
                assert event.context["work_type"] == "task"
                assert event.outcome["duration_seconds"] == 12.5
        finally:
            await cleanup_learning_events([event_id])
            await cleanup_test_agent(agent.id)

    async def test_heartbeat_no_work(self):
        agent = await create_test_agent()
        try:
            event_id = await capture_heartbeat(
                agent_name=agent.name,
                found_work=False,
                work_type=None,
                duration_seconds=0.05,
            )
            async with AsyncSessionLocal() as s:
                event = (await s.execute(
                    select(LearningEvent).where(LearningEvent.id == event_id)
                )).scalar_one()
                assert event.context["found_work"] is False
                assert event.context["work_type"] is None
        finally:
            await cleanup_learning_events([event_id])
            await cleanup_test_agent(agent.id)


# ============================================================
# capture_task_outcome
# ============================================================

class TestCaptureTaskOutcome:

    async def test_successful_task(self):
        agent = await create_test_agent()
        task = await create_test_task()
        try:
            event_id = await capture_task_outcome(
                agent_name=agent.name,
                task_id=str(task.id),
                task_title=task.title,
                from_status="assigned",
                to_status="review",
                duration_seconds=45.0,
                success=True,
                response_preview="Completed the implementation",
            )
            async with AsyncSessionLocal() as s:
                event = (await s.execute(
                    select(LearningEvent).where(LearningEvent.id == event_id)
                )).scalar_one()
                assert event.event_type == "task_outcome"
                assert event.context["task_id"] == str(task.id)
                assert event.outcome["success"] is True
                assert event.outcome["duration_seconds"] == 45.0
        finally:
            await cleanup_learning_events([event_id])
            await cleanup_test_task(task.id)
            await cleanup_test_agent(agent.id)

    async def test_failed_task(self):
        agent = await create_test_agent()
        task = await create_test_task()
        try:
            event_id = await capture_task_outcome(
                agent_name=agent.name,
                task_id=str(task.id),
                task_title=task.title,
                from_status="in_progress",
                to_status="review",
                duration_seconds=300.0,
                success=False,
                error="TimeoutError: agent hung",
            )
            async with AsyncSessionLocal() as s:
                event = (await s.execute(
                    select(LearningEvent).where(LearningEvent.id == event_id)
                )).scalar_one()
                assert event.outcome["success"] is False
                assert "TimeoutError" in event.outcome["error"]
        finally:
            await cleanup_learning_events([event_id])
            await cleanup_test_task(task.id)
            await cleanup_test_agent(agent.id)


# ============================================================
# capture_tool_usage
# ============================================================

class TestCaptureToolUsage:

    async def test_successful_tool(self):
        agent = await create_test_agent()
        try:
            event_id = await capture_tool_usage(
                agent_name=agent.name,
                tool_name="create_task",
                tool_args={"title": "New task"},
                success=True,
                duration_seconds=0.3,
            )
            async with AsyncSessionLocal() as s:
                event = (await s.execute(
                    select(LearningEvent).where(LearningEvent.id == event_id)
                )).scalar_one()
                assert event.event_type == "tool_usage"
                assert event.context["tool_name"] == "create_task"
                assert event.outcome["success"] is True
        finally:
            await cleanup_learning_events([event_id])
            await cleanup_test_agent(agent.id)

    async def test_failed_tool(self):
        agent = await create_test_agent()
        try:
            event_id = await capture_tool_usage(
                agent_name=agent.name,
                tool_name="update_task_status",
                tool_args={"task_title": "x", "new_status": "invalid"},
                success=False,
                duration_seconds=0.1,
                error="Invalid status",
            )
            async with AsyncSessionLocal() as s:
                event = (await s.execute(
                    select(LearningEvent).where(LearningEvent.id == event_id)
                )).scalar_one()
                assert event.outcome["success"] is False
                assert event.outcome["error"] == "Invalid status"
        finally:
            await cleanup_learning_events([event_id])
            await cleanup_test_agent(agent.id)


# ============================================================
# capture_error_fix (pattern creation)
# ============================================================

class TestCaptureErrorFix:

    async def test_creates_pattern(self):
        await capture_error_fix(
            trigger="test_tz_naive",
            error_context={"error": "naive datetime"},
            resolution={"fix": "use utcnow()"},
        )
        try:
            async with AsyncSessionLocal() as s:
                pattern = (await s.execute(
                    select(LearningPattern).where(
                        LearningPattern.trigger_text == "test_tz_naive"
                    )
                )).scalar_one()
                assert pattern.type == LearningType.ERROR_FIX
                assert pattern.confidence == 0.6
                assert pattern.occurrence_count == 1
                pid = pattern.id
        finally:
            await cleanup_pattern(pid)


# ============================================================
# Pattern confidence adjustment
# ============================================================

class TestPatternConfidence:

    async def test_confidence_increases_on_success(self):
        async with AsyncSessionLocal() as s:
            pattern = LearningPattern(
                type=LearningType.TOOL_USAGE,
                trigger_text="test_conf_up",
                context={"intent": "test"},
                resolution={"tool": "test_tool"},
                confidence=0.5,
                occurrence_count=1,
            )
            s.add(pattern)
            await s.commit()
            pid = pattern.id

        try:
            await update_pattern_usage(pid, success=True)

            async with AsyncSessionLocal() as s:
                p = (await s.execute(
                    select(LearningPattern).where(LearningPattern.id == pid)
                )).scalar_one()
                assert p.confidence == pytest.approx(0.55)
                assert p.occurrence_count == 2
        finally:
            await cleanup_pattern(pid)

    async def test_confidence_decreases_on_failure(self):
        async with AsyncSessionLocal() as s:
            pattern = LearningPattern(
                type=LearningType.TOOL_USAGE,
                trigger_text="test_conf_down",
                context={"intent": "test"},
                resolution={"tool": "test_tool"},
                confidence=0.5,
                occurrence_count=1,
            )
            s.add(pattern)
            await s.commit()
            pid = pattern.id

        try:
            await update_pattern_usage(pid, success=False)

            async with AsyncSessionLocal() as s:
                p = (await s.execute(
                    select(LearningPattern).where(LearningPattern.id == pid)
                )).scalar_one()
                assert p.confidence == pytest.approx(0.4)
                assert p.occurrence_count == 2
        finally:
            await cleanup_pattern(pid)


# ============================================================
# Integration
# ============================================================

class TestIntegration:

    async def test_multiple_events_accumulate(self):
        agent = await create_test_agent()
        ids = []
        try:
            ids.append(await capture_heartbeat(
                agent_name=agent.name,
                found_work=True, work_type="task", duration_seconds=60.0,
            ))
            ids.append(await capture_task_outcome(
                agent_name=agent.name,
                task_id=str(uuid.uuid4()), task_title="Test integration",
                from_status="assigned", to_status="review",
                duration_seconds=55.0, success=True,
            ))
            ids.append(await capture_tool_usage(
                agent_name=agent.name,
                tool_name="list_tasks", tool_args={},
                success=True, duration_seconds=0.2,
            ))

            async with AsyncSessionLocal() as s:
                count = (await s.execute(
                    select(func.count()).select_from(LearningEvent).where(
                        LearningEvent.id.in_(ids)
                    )
                )).scalar()
                assert count == 3
        finally:
            await cleanup_learning_events(ids)
            await cleanup_test_agent(agent.id)

    async def test_capture_is_resilient(self):
        """Capture should not crash on unknown agent."""
        event_id = await capture_learning_event(
            agent_name="Nobody",
            event_type="test_resilient",
            context={"test": True},
        )
        await cleanup_learning_events([event_id])
