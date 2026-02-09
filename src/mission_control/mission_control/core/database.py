"""
Database models and connection management for Mission Control.
Supports both PostgreSQL and SQLite backends.
"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    event,
)
from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from mission_control.config import settings

# ===========================================
# Portable UUID type (works on both PG and SQLite)
# ===========================================

class PortableUUID(TypeDecorator):
    """UUID stored as String(36) on SQLite, native UUID on PostgreSQL."""
    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID
            return dialect.type_descriptor(UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _is_sqlite() -> bool:
    return settings.database_url.startswith("sqlite")


# ===========================================
# Enums
# ===========================================

class AgentStatus(str, Enum):
    IDLE = "idle"
    ACTIVE = "active"
    BLOCKED = "blocked"
    ERROR = "error"


class AgentLevel(str, Enum):
    INTERN = "intern"        # Needs approval for most actions
    SPECIALIST = "specialist" # Works independently in their domain
    LEAD = "lead"            # Full autonomy, can delegate


class TaskStatus(str, Enum):
    INBOX = "inbox"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"


class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class ActivityType(str, Enum):
    TASK_CREATED = "task_created"
    TASK_ASSIGNED = "task_assigned"
    TASK_STATUS_CHANGED = "task_status_changed"
    MESSAGE_SENT = "message_sent"
    DOCUMENT_CREATED = "document_created"
    AGENT_HEARTBEAT = "agent_heartbeat"
    LEARNING_CAPTURED = "learning_captured"
    MESSAGE_RECEIVED = "message_received"
    MESSAGE_RESPONDED = "message_responded"


class LearningType(str, Enum):
    ERROR_FIX = "error_fix"
    TOOL_USAGE = "tool_usage"
    WORKFLOW = "workflow"
    INTENT = "intent"


# ===========================================
# Base
# ===========================================

def utcnow() -> datetime:
    """Timezone-aware UTC now. Replaces datetime.utcnow() which asyncpg
    misinterprets as local time for TIMESTAMP WITH TIME ZONE columns."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ===========================================
# Models
# ===========================================

class Agent(Base):
    """Agent definition and state."""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(PortableUUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    session_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    status: Mapped[AgentStatus] = mapped_column(SQLEnum(AgentStatus), default=AgentStatus.IDLE)
    level: Mapped[AgentLevel] = mapped_column(
        SQLEnum(AgentLevel, values_callable=lambda x: [e.value for e in x]),
        default=AgentLevel.SPECIALIST,
    )
    current_task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PortableUUID(), ForeignKey("tasks.id"), nullable=True
    )
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_offset_minutes: Mapped[int] = mapped_column(Integer, default=0)  # Stagger offset
    mcp_servers: Mapped[Optional[list]] = mapped_column(JSON, default=list)  # List of MCP server names
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    tasks_assigned = relationship("TaskAssignment", back_populates="agent")
    messages = relationship("Message", back_populates="from_agent")
    activities = relationship("Activity", back_populates="agent")
    notifications = relationship("Notification", back_populates="mentioned_agent")


class Task(Base):
    """Task/work item."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(PortableUUID(), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(SQLEnum(TaskStatus), default=TaskStatus.INBOX)
    priority: Mapped[TaskPriority] = mapped_column(SQLEnum(TaskPriority), default=TaskPriority.MEDIUM)
    mission_type: Mapped[str] = mapped_column(
        String(30), default="build", server_default="build", nullable=False,
    )
    mission_config: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False,
    )
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PortableUUID(), ForeignKey("agents.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    # Relationships
    assignments = relationship("TaskAssignment", back_populates="task")
    messages = relationship("Message", back_populates="task")
    documents = relationship("Document", back_populates="task")
    subscriptions = relationship("ThreadSubscription", back_populates="task")


class TaskAssignment(Base):
    """Many-to-many relationship between tasks and agents."""

    __tablename__ = "task_assignments"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PortableUUID(), ForeignKey("tasks.id"), primary_key=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PortableUUID(), ForeignKey("agents.id"), primary_key=True
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    task = relationship("Task", back_populates="assignments")
    agent = relationship("Agent", back_populates="tasks_assigned")


class Message(Base):
    """Comment/message on a task."""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(PortableUUID(), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(PortableUUID(), ForeignKey("tasks.id"), nullable=False)
    from_agent_id: Mapped[uuid.UUID] = mapped_column(
        PortableUUID(), ForeignKey("agents.id"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    task = relationship("Task", back_populates="messages")
    from_agent = relationship("Agent", back_populates="messages")
    notifications = relationship("Notification", back_populates="source_message")


class Activity(Base):
    """Activity feed entry."""

    __tablename__ = "activities"

    id: Mapped[uuid.UUID] = mapped_column(PortableUUID(), primary_key=True, default=uuid.uuid4)
    type: Mapped[ActivityType] = mapped_column(SQLEnum(ActivityType), nullable=False)
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PortableUUID(), ForeignKey("agents.id"), nullable=True
    )
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PortableUUID(), ForeignKey("tasks.id"), nullable=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    agent = relationship("Agent", back_populates="activities")


class Document(Base):
    """Deliverable or research document."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(PortableUUID(), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String(50), nullable=True)  # deliverable, research, protocol, runbook
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PortableUUID(), ForeignKey("tasks.id"), nullable=True
    )
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PortableUUID(), ForeignKey("agents.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    # Relationships
    task = relationship("Task", back_populates="documents")


class Notification(Base):
    """@mention notification."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(PortableUUID(), primary_key=True, default=uuid.uuid4)
    mentioned_agent_id: Mapped[uuid.UUID] = mapped_column(
        PortableUUID(), ForeignKey("agents.id"), nullable=False
    )
    source_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PortableUUID(), ForeignKey("messages.id"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    mentioned_agent = relationship("Agent", back_populates="notifications")
    source_message = relationship("Message", back_populates="notifications")


class ThreadSubscription(Base):
    """Agent subscription to a task thread."""

    __tablename__ = "thread_subscriptions"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PortableUUID(), ForeignKey("tasks.id"), primary_key=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PortableUUID(), ForeignKey("agents.id"), primary_key=True
    )
    subscribed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationships
    task = relationship("Task", back_populates="subscriptions")


class LearningPattern(Base):
    """Learned pattern from execution or monitoring."""

    __tablename__ = "learning_patterns"

    id: Mapped[uuid.UUID] = mapped_column(PortableUUID(), primary_key=True, default=uuid.uuid4)
    type: Mapped[LearningType] = mapped_column(SQLEnum(LearningType), nullable=False)
    mission_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # Vector embedding — pgvector on PostgreSQL, Text on SQLite (no similarity search)
    if _is_sqlite():
        trigger_embedding = mapped_column(Text, nullable=True)
    else:
        from pgvector.sqlalchemy import Vector
        trigger_embedding = mapped_column(Vector(1536), nullable=True)
    trigger_text: Mapped[str] = mapped_column(Text, nullable=False)  # Human-readable trigger
    context: Mapped[dict] = mapped_column(JSON, nullable=False)
    resolution: Mapped[dict] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    last_used: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LearningEvent(Base):
    """Raw learning event before aggregation."""

    __tablename__ = "learning_events"

    id: Mapped[uuid.UUID] = mapped_column(PortableUUID(), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PortableUUID(), ForeignKey("agents.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)  # error, success, anomaly
    mission_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    context: Mapped[dict] = mapped_column(JSON, nullable=False)
    outcome: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ===========================================
# Database Connection
# ===========================================

def _create_engine():
    """Create async engine appropriate for the configured backend."""
    url = settings.database_url_async
    if _is_sqlite():
        engine = create_async_engine(url, echo=not settings.is_production)

        # SQLite: enable WAL mode + foreign keys on every connection
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    # PostgreSQL: connection pool + UTC timezone
    return create_async_engine(
        url,
        echo=not settings.is_production,
        pool_size=5,
        max_overflow=10,
        connect_args={"server_settings": {"timezone": "UTC"}},
    )


async_engine = _create_engine()

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncSession:
    """Dependency for getting database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Initialize database tables."""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    """Close database connections."""
    await async_engine.dispose()
