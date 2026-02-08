"""
HTTP API for Mission Control.

Provides a simple REST API to interact with agents without Telegram.
"""

import os
from datetime import datetime, timezone

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agents.mission_control.core.factory import AgentFactory

logger = structlog.get_logger()

app = FastAPI(
    title="Mission Control API",
    description="HTTP API for interacting with the Mission Control agent squad",
    version="0.1.0",
)

# CORS — restrict in production; override via CORS_ORIGINS env var
_cors_origins = os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://localhost:8000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================
# Request/Response Models
# ===========================================

class ChatRequest(BaseModel):
    message: str
    agent: str = "jarvis"  # Default to Jarvis


class ChatResponse(BaseModel):
    agent: str
    response: str
    timestamp: str


class TaskRequest(BaseModel):
    title: str
    description: str = ""
    assignees: list[str] = []
    priority: str = "medium"


class TaskResponse(BaseModel):
    task_id: str
    title: str
    assignees: list[str]


class AgentStatus(BaseModel):
    name: str
    role: str
    mcp_servers: list[str]
    status: str = "ready"


# ===========================================
# API Endpoints
# ===========================================

@app.get("/")
async def root():
    """Health check and welcome message."""
    return {
        "service": "Mission Control",
        "status": "running",
        "version": "0.1.0",
        "agents": len(AgentFactory.list_agents()),
    }


@app.get("/agents", response_model=list[AgentStatus])
async def list_agents():
    """List all available agents."""
    return [
        AgentStatus(
            name=a["name"],
            role=a["role"],
            mcp_servers=a["mcp_servers"],
        )
        for a in AgentFactory.list_agents()
    ]


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Send a message to an agent and get a response.

    This is the main endpoint for interacting with agents.
    Default agent is Jarvis (the coordinator).

    Example:
        curl -X POST http://localhost:8000/chat \
            -H "Content-Type: application/json" \
            -d '{"message": "Create a task to fix the login bug"}'
    """
    try:
        agent = AgentFactory.get_agent(request.agent)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    logger.info(
        "Chat request",
        agent=request.agent,
        message_preview=request.message[:100],
    )

    try:
        response = await agent.run(request.message)

        return ChatResponse(
            agent=agent.name,
            response=response,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        logger.error("Chat error", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/chat/{agent_name}", response_model=ChatResponse)
async def chat_with_agent(agent_name: str, request: ChatRequest):
    """
    Send a message to a specific agent.

    Example:
        curl -X POST http://localhost:8000/chat/friday \
            -H "Content-Type: application/json" \
            -d '{"message": "Review the authentication code"}'
    """
    request.agent = agent_name
    return await chat(request)


@app.post("/task", response_model=TaskResponse)
async def create_task(request: TaskRequest):
    """
    Create a new task via Jarvis.

    Example:
        curl -X POST http://localhost:8000/task \
            -H "Content-Type: application/json" \
            -d '{"title": "Fix login bug", "assignees": ["friday"]}'
    """
    from agents.squad.jarvis.agent import create_jarvis

    jarvis = create_jarvis()

    try:
        task_id = await jarvis.create_task(
            title=request.title,
            description=request.description,
            assignees=request.assignees,
            priority=request.priority,
        )

        return TaskResponse(
            task_id=task_id,
            title=request.title,
            assignees=request.assignees,
        )
    except Exception as e:
        logger.error("Task creation error", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/standup")
async def daily_standup():
    """
    Get daily standup summary.

    Example:
        curl http://localhost:8000/standup
    """
    from agents.squad.jarvis.agent import create_jarvis

    jarvis = create_jarvis()
    summary = await jarvis.generate_daily_standup()

    return {
        "standup": summary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/heartbeat/{agent_name}")
async def trigger_heartbeat(agent_name: str):
    """
    Manually trigger a heartbeat for an agent.

    Example:
        curl -X POST http://localhost:8000/heartbeat/friday
    """
    try:
        agent = AgentFactory.get_agent(agent_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    result = await agent.heartbeat()

    return {
        "agent": agent.name,
        "result": result,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ===========================================
# Dashboard Endpoints (for Kanban UI)
# ===========================================

@app.get("/dashboard/agents")
async def dashboard_agents():
    """All agents with DB status and heartbeat info."""
    from sqlalchemy import select

    from agents.mission_control.core.database import Agent as AgentModel
    from agents.mission_control.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(AgentModel).order_by(AgentModel.name))
        agents = result.scalars().all()
        return [
            {
                "name": a.name,
                "role": a.role,
                "status": a.status.value if hasattr(a.status, 'value') else str(a.status),
                "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
                "mcp_servers": a.mcp_servers or [],
            }
            for a in agents
        ]


@app.get("/dashboard/tasks")
async def dashboard_tasks():
    """All tasks with assignees and ETA for assigned tasks."""
    from sqlalchemy import select

    from agents.mission_control.core.database import (
        Agent as AgentModel,
    )
    from agents.mission_control.core.database import (
        AsyncSessionLocal,
        LearningEvent,
        Task,
        TaskAssignment,
        TaskStatus,
    )
    from agents.mission_control.scheduler.heartbeat import AGENT_SCHEDULE

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).order_by(Task.priority.desc(), Task.created_at)
        )
        tasks = result.scalars().all()

        # Pre-fetch: avg heartbeat duration per agent from learning_events
        hb_events = (await session.execute(
            select(AgentModel.name, LearningEvent.outcome)
            .join(LearningEvent, LearningEvent.agent_id == AgentModel.id)
            .where(LearningEvent.event_type == "heartbeat")
        )).all()
        # Compute avg duration for heartbeats that found work
        agent_durations = {}
        for name, outcome in hb_events:
            if outcome and outcome.get("found_work"):
                dur = outcome.get("duration_seconds", 0)
                if dur and dur > 0:
                    agent_durations.setdefault(name, []).append(dur)
        avg_task_dur = {n: sum(ds)/len(ds) for n, ds in agent_durations.items()}

        # Pre-fetch: last heartbeat per agent
        hb_rows = (await session.execute(
            select(AgentModel.name, AgentModel.last_heartbeat)
        )).all()
        last_hb = {name: hb for name, hb in hb_rows}

        # Pre-fetch: queue positions — all assigned tasks per agent ordered by created_at
        queue_rows = (await session.execute(
            select(AgentModel.name, Task.id)
            .join(TaskAssignment, TaskAssignment.agent_id == AgentModel.id)
            .join(Task, Task.id == TaskAssignment.task_id)
            .where(Task.status == TaskStatus.ASSIGNED)
            .order_by(Task.priority.desc(), Task.created_at)
        )).all()
        # Build queue position map: {task_id: (agent_name, position)}
        agent_queues = {}
        for agent_name, task_id in queue_rows:
            if agent_name not in agent_queues:
                agent_queues[agent_name] = []
            agent_queues[agent_name].append(task_id)

        queue_pos = {}
        for agent_name, task_ids in agent_queues.items():
            for i, tid in enumerate(task_ids):
                queue_pos[tid] = (agent_name, i)

        # In-progress tasks per agent
        ip_rows = (await session.execute(
            select(AgentModel.name)
            .join(TaskAssignment, TaskAssignment.agent_id == AgentModel.id)
            .join(Task, Task.id == TaskAssignment.task_id)
            .where(Task.status == TaskStatus.IN_PROGRESS)
        )).all()
        agents_busy = {r[0] for r in ip_rows}

        now = datetime.now(timezone.utc)

        task_list = []
        for t in tasks:
            assign_result = await session.execute(
                select(AgentModel.name)
                .join(TaskAssignment, TaskAssignment.agent_id == AgentModel.id)
                .where(TaskAssignment.task_id == t.id)
            )
            assignees = [row[0] for row in assign_result]

            # Compute ETA for assigned tasks
            eta_info = None
            status_val = t.status.value if hasattr(t.status, 'value') else str(t.status)
            if status_val == "assigned" and assignees:
                agent_name = assignees[0]
                agent_key = agent_name.lower()
                offset = AGENT_SCHEDULE.get(agent_key, 0)

                # Next heartbeat: offset pattern is :offset, :offset+15, :offset+30, :offset+45
                slots = sorted(set((offset + i*15) % 60 for i in range(4)))
                current_min = now.minute
                next_slot = None
                for s in slots:
                    if s > current_min:
                        next_slot = s
                        break
                if next_slot is None:
                    next_slot = slots[0]  # wraps to next hour
                mins_to_next = (next_slot - current_min) % 60
                if mins_to_next == 0:
                    mins_to_next = 15  # just fired, next in 15

                # Queue position
                pos_info = queue_pos.get(t.id)
                pos = pos_info[1] if pos_info else 0

                # Avg task duration for this agent (seconds → minutes)
                avg_dur_min = avg_task_dur.get(agent_name, 120) / 60  # default 2min

                # ETA = next heartbeat + (queue_pos * 15min cycle) + busy penalty
                busy_penalty = avg_dur_min if agent_name in agents_busy else 0
                eta_minutes = round(mins_to_next + (pos * 15) + busy_penalty)

                eta_info = {
                    "minutes": eta_minutes,
                    "queue_position": pos + 1,
                    "queue_size": len(agent_queues.get(agent_name, [])),
                    "agent_busy": agent_name in agents_busy,
                    "next_heartbeat_min": mins_to_next,
                }

            task_list.append({
                "id": str(t.id),
                "title": t.title,
                "description": t.description or "",
                "status": status_val,
                "priority": t.priority.value if hasattr(t.priority, 'value') else str(t.priority),
                "assignees": assignees,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "eta": eta_info,
            })
        return task_list


@app.get("/dashboard/activities")
async def dashboard_activities():
    """Recent activities (last 24h)."""
    from datetime import timedelta

    from sqlalchemy import desc, select

    from agents.mission_control.core.database import (
        Activity,
        AsyncSessionLocal,
    )
    from agents.mission_control.core.database import (
        Agent as AgentModel,
    )

    async with AsyncSessionLocal() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        result = await session.execute(
            select(Activity, AgentModel.name)
            .outerjoin(AgentModel, Activity.agent_id == AgentModel.id)
            .where(Activity.created_at >= cutoff)
            .order_by(desc(Activity.created_at))
            .limit(100)
        )
        rows = result.all()
        return [
            {
                "type": a.type.value if hasattr(a.type, 'value') else str(a.type),
                "agent": agent_name,
                "message": a.message,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a, agent_name in rows
        ]


# Serve kanban dashboard
@app.get("/dashboard")
async def dashboard_page():
    """Serve the Mission Control kanban dashboard."""
    import os
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    return FileResponse(html_path, media_type="text/html")


# ===========================================
# Learning Analytics Endpoints
# ===========================================

@app.get("/dashboard/learning/stats")
async def learning_stats():
    """Summary stats: totals by event type, by agent, overall counts."""
    from sqlalchemy import func, select

    from agents.mission_control.core.database import Agent as AgentModel
    from agents.mission_control.core.database import (
        AsyncSessionLocal,
        LearningEvent,
        LearningPattern,
    )

    async with AsyncSessionLocal() as session:
        # Total events
        total = (await session.execute(
            select(func.count()).select_from(LearningEvent)
        )).scalar() or 0

        # By event type
        rows = (await session.execute(
            select(LearningEvent.event_type, func.count())
            .group_by(LearningEvent.event_type)
        )).all()
        by_type = {r[0]: r[1] for r in rows}

        # By agent
        rows = (await session.execute(
            select(AgentModel.name, func.count())
            .join(LearningEvent, LearningEvent.agent_id == AgentModel.id)
            .group_by(AgentModel.name)
            .order_by(func.count().desc())
        )).all()
        by_agent = {r[0]: r[1] for r in rows}

        # Pattern count
        pattern_count = (await session.execute(
            select(func.count()).select_from(LearningPattern)
        )).scalar() or 0

        # Avg heartbeat duration
        avg_hb = (await session.execute(
            select(func.avg(LearningEvent.outcome["duration_seconds"].as_float()))
            .where(LearningEvent.event_type == "heartbeat")
        )).scalar()

        # Task success rate
        task_total = (await session.execute(
            select(func.count()).select_from(LearningEvent)
            .where(LearningEvent.event_type == "task_outcome")
        )).scalar() or 0
        task_success = (await session.execute(
            select(func.count()).select_from(LearningEvent)
            .where(
                LearningEvent.event_type == "task_outcome",
                LearningEvent.outcome["success"].as_boolean() == True,
            )
        )).scalar() or 0

        return {
            "total_events": total,
            "by_type": by_type,
            "by_agent": by_agent,
            "pattern_count": pattern_count,
            "avg_heartbeat_seconds": round(avg_hb, 3) if avg_hb else 0,
            "task_success_rate": round(task_success / task_total, 2) if task_total else None,
            "task_total": task_total,
        }


@app.get("/dashboard/learning/timeline")
async def learning_timeline(hours: int = 24):
    """Event counts grouped by hour for time-series charts."""
    from datetime import timedelta

    from sqlalchemy import func, select

    from agents.mission_control.core.database import AsyncSessionLocal, LearningEvent

    async with AsyncSessionLocal() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        rows = (await session.execute(
            select(
                func.date_trunc("hour", LearningEvent.created_at).label("hour"),
                LearningEvent.event_type,
                func.count(),
            )
            .where(LearningEvent.created_at >= cutoff)
            .group_by("hour", LearningEvent.event_type)
            .order_by("hour")
        )).all()

        timeline = {}
        for hour, event_type, count in rows:
            h = hour.isoformat()
            if h not in timeline:
                timeline[h] = {}
            timeline[h][event_type] = count

        return {"hours": hours, "data": timeline}


@app.get("/dashboard/learning/agents")
async def learning_agent_metrics():
    """Per-agent performance: heartbeat freq, avg duration, task outcomes."""
    from sqlalchemy import case, func, select

    from agents.mission_control.core.database import Agent as AgentModel
    from agents.mission_control.core.database import AsyncSessionLocal, LearningEvent

    async with AsyncSessionLocal() as session:
        # Heartbeat stats per agent
        hb_rows = (await session.execute(
            select(
                AgentModel.name,
                func.count(),
                func.avg(LearningEvent.outcome["duration_seconds"].as_float()),
                func.max(LearningEvent.created_at),
            )
            .join(LearningEvent, LearningEvent.agent_id == AgentModel.id)
            .where(LearningEvent.event_type == "heartbeat")
            .group_by(AgentModel.name)
        )).all()

        # Task stats per agent
        task_rows = (await session.execute(
            select(
                AgentModel.name,
                func.count(),
                func.sum(case(
                    (LearningEvent.outcome["success"].as_boolean() == True, 1),
                    else_=0,
                )),
                func.avg(LearningEvent.outcome["duration_seconds"].as_float()),
            )
            .join(LearningEvent, LearningEvent.agent_id == AgentModel.id)
            .where(LearningEvent.event_type == "task_outcome")
            .group_by(AgentModel.name)
        )).all()
        task_map = {r[0]: {"total": r[1], "success": r[2], "avg_duration": r[3]} for r in task_rows}

        # Error counts per agent
        err_rows = (await session.execute(
            select(AgentModel.name, func.count())
            .join(LearningEvent, LearningEvent.agent_id == AgentModel.id)
            .where(LearningEvent.event_type == "error")
            .group_by(AgentModel.name)
        )).all()
        err_map = {r[0]: r[1] for r in err_rows}

        agents = []
        for name, hb_count, avg_dur, last_hb in hb_rows:
            t = task_map.get(name, {})
            agents.append({
                "name": name,
                "heartbeats": hb_count,
                "avg_heartbeat_sec": round(avg_dur, 3) if avg_dur else 0,
                "last_heartbeat": last_hb.isoformat() if last_hb else None,
                "tasks_total": t.get("total", 0),
                "tasks_success": t.get("success", 0),
                "tasks_avg_duration": round(t["avg_duration"], 1) if t.get("avg_duration") else 0,
                "errors": err_map.get(name, 0),
            })
        return agents


@app.get("/dashboard/learning/events")
async def learning_events(limit: int = 50, event_type: str = None, agent: str = None):
    """Recent learning events with optional filters."""
    from sqlalchemy import desc, select

    from agents.mission_control.core.database import Agent as AgentModel
    from agents.mission_control.core.database import AsyncSessionLocal, LearningEvent

    async with AsyncSessionLocal() as session:
        stmt = (
            select(LearningEvent, AgentModel.name)
            .outerjoin(AgentModel, LearningEvent.agent_id == AgentModel.id)
            .order_by(desc(LearningEvent.created_at))
            .limit(min(limit, 200))
        )
        if event_type:
            stmt = stmt.where(LearningEvent.event_type == event_type)
        if agent:
            stmt = stmt.where(AgentModel.name.ilike(agent))

        rows = (await session.execute(stmt)).all()
        return [
            {
                "id": str(e.id),
                "agent": aname,
                "event_type": e.event_type,
                "context": e.context,
                "outcome": e.outcome,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e, aname in rows
        ]


@app.get("/dashboard/learning/patterns")
async def learning_patterns():
    """All learning patterns with confidence and usage stats."""
    from sqlalchemy import desc, select

    from agents.mission_control.core.database import AsyncSessionLocal, LearningPattern

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(LearningPattern).order_by(desc(LearningPattern.confidence))
        )).scalars().all()
        return [
            {
                "id": str(p.id),
                "type": p.type.value if hasattr(p.type, "value") else str(p.type),
                "trigger": p.trigger_text,
                "confidence": p.confidence,
                "occurrences": p.occurrence_count,
                "last_used": p.last_used.isoformat() if p.last_used else None,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in rows
        ]


# Serve learning dashboard
@app.get("/dashboard/learning")
async def learning_dashboard_page():
    """Serve the learning analytics dashboard."""
    import os
    html_path = os.path.join(os.path.dirname(__file__), "static", "learning.html")
    return FileResponse(html_path, media_type="text/html")


# ===========================================
# Run Server
# ===========================================

def run_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the API server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
