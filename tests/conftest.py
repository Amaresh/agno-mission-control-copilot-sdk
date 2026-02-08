"""Shared fixtures for Mission Control tests."""

import uuid
from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from agents.mission_control.core.database import (
    Agent,
    AgentStatus,
    AgentLevel,
    Task,
    TaskStatus,
    TaskPriority,
    LearningEvent,
    LearningPattern,
)
from agents.config import settings
from sqlalchemy import delete

# Test engine: NullPool so every session gets a dedicated connection
_test_engine = create_async_engine(
    settings.database_url_async,
    echo=False,
    poolclass=NullPool,
)
TestSession = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False,
)

# Monkey-patch the production session factory so capture.py uses NullPool too
import agents.mission_control.learning.capture as _capture_mod
import agents.mission_control.core.database as _db_mod
_capture_mod.AsyncSessionLocal = TestSession
_db_mod.AsyncSessionLocal = TestSession


async def create_test_agent(name: str = None) -> object:
    """Create a temporary agent in the DB."""
    agent_id = uuid.uuid4()
    name = name or f"TestAgent-{uuid.uuid4().hex[:6]}"
    async with TestSession() as s:
        agent = Agent(
            id=agent_id, name=name, role="Developer",
            session_key=f"agent:test:{uuid.uuid4().hex[:6]}",
            status=AgentStatus.ACTIVE, level=AgentLevel.SPECIALIST,
            last_heartbeat=datetime.now(timezone.utc),
        )
        s.add(agent)
        await s.commit()
    return type("FakeAgent", (), {"id": agent_id, "name": name})()


async def create_test_task(title: str = None) -> object:
    """Create a temporary task in the DB."""
    task_id = uuid.uuid4()
    title = title or f"Test Task {uuid.uuid4().hex[:6]}"
    async with TestSession() as s:
        task = Task(
            id=task_id, title=title, description="A test task",
            status=TaskStatus.ASSIGNED, priority=TaskPriority.MEDIUM,
        )
        s.add(task)
        await s.commit()
    return type("FakeTask", (), {"id": task_id, "title": title})()


async def cleanup_test_agent(agent_id: uuid.UUID):
    async with TestSession() as s:
        await s.execute(delete(LearningEvent).where(LearningEvent.agent_id == agent_id))
        await s.execute(delete(Agent).where(Agent.id == agent_id))
        await s.commit()


async def cleanup_test_task(task_id: uuid.UUID):
    async with TestSession() as s:
        await s.execute(delete(Task).where(Task.id == task_id))
        await s.commit()


async def cleanup_learning_events(event_ids: list[uuid.UUID]):
    async with TestSession() as s:
        await s.execute(delete(LearningEvent).where(LearningEvent.id.in_(event_ids)))
        await s.commit()


async def cleanup_pattern(pattern_id: uuid.UUID):
    async with TestSession() as s:
        await s.execute(delete(LearningPattern).where(LearningPattern.id == pattern_id))
        await s.commit()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def sweep_test_data():
    """Safety net: remove any TestAgent-* rows left behind by crashed tests."""
    yield
    async with TestSession() as s:
        from sqlalchemy import select
        ids = (await s.execute(
            select(Agent.id).where(Agent.name.ilike("TestAgent%"))
        )).scalars().all()
        if ids:
            await s.execute(delete(LearningEvent).where(LearningEvent.agent_id.in_(ids)))
            await s.execute(delete(Agent).where(Agent.id.in_(ids)))
        await s.execute(delete(LearningPattern).where(LearningPattern.trigger_text.ilike("test_%")))
        await s.execute(delete(Task).where(Task.title.ilike("Test Task%")))
        await s.commit()
