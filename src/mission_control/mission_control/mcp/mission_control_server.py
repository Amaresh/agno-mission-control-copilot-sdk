"""
Mission Control MCP Server.

Exposes Mission Control tools as an MCP server for use with Copilot SDK.
This allows the Copilot model to natively call Mission Control operations
like creating tasks, listing tasks, delegating work, etc.

Run standalone: python -m agents.mission_control.mcp.mission_control_server
"""

import json
import logging
import os
import sys

# Ensure project root is in path for imports
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from mcp.server.fastmcp import FastMCP

# Configure logging to stderr (required for MCP servers on stdio)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("mission_control_mcp")

# Initialize MCP server
MCP_PORT = int(os.environ.get("MCP_PORT", "8001"))
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "sse")

mcp = FastMCP("mission-control", host="127.0.0.1", port=MCP_PORT)


# ============================================================================
# Tool usage capture wrapper
# ============================================================================

import functools
import time


def _capture_tool(fn):
    """Decorator that captures MCP tool usage into learning_events."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        t0 = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
            # Fire-and-forget capture (don't delay the response)
            try:
                from mission_control.mission_control.learning.capture import capture_tool_usage
                agent_name = kwargs.get("agent_name", "unknown")
                await capture_tool_usage(
                    agent_name=agent_name,
                    tool_name=fn.__name__,
                    tool_args=kwargs,
                    success=True,
                    duration_seconds=time.monotonic() - t0,
                )
            except Exception:
                pass  # never crash the tool
            return result
        except Exception as e:
            try:
                from mission_control.mission_control.learning.capture import capture_tool_usage
                agent_name = kwargs.get("agent_name", "unknown")
                await capture_tool_usage(
                    agent_name=agent_name,
                    tool_name=fn.__name__,
                    tool_args=kwargs,
                    success=False,
                    duration_seconds=time.monotonic() - t0,
                    error=str(e)[:300],
                )
            except Exception:
                pass
            raise
    return wrapper


# ============================================================================
# Mission Control Tools
# ============================================================================

@mcp.tool()
@_capture_tool
async def create_task(
    title: str,
    description: str = "",
    assignees: str = "",
    priority: str = "medium",
    repository: str = "",
) -> str:
    """
    Create a new task in Mission Control and optionally assign it to agents.
    
    Args:
        title: The task title (required)
        description: Detailed description of the task
        assignees: Comma-separated agent names to assign (e.g., "friday,shuri")
        priority: Task priority - low, medium, high, or critical
        repository: Target GitHub repository (e.g., "{owner}/{repo}"). 
                    REQUIRED. You MUST specify which repo the work targets.
                    Ask the human if you don't know which repository to use.
    
    Returns:
        Confirmation message with task details
    """
    from sqlalchemy import select

    from mission_control.mission_control.core.database import (
        Activity,
        ActivityType,
        AsyncSessionLocal,
        Task,
        TaskAssignment,
        TaskPriority,
        TaskStatus,
    )
    from mission_control.mission_control.core.database import (
        Agent as AgentModel,
    )

    assignees = assignees or ""
    priority = priority or "medium"
    description = description or ""
    repository = (repository or "").strip()

    # ENFORCE: repository is required
    if not repository:
        return (
            "ERROR: `repository` parameter is REQUIRED. "
            "You must specify the target GitHub repository (e.g., '{owner}/{repo}'). "
            "If you don't know which repo this work targets, ASK THE HUMAN before creating the task."
        )

    # Prepend repository context to description if provided
    if repository and "Repository:" not in description:
        description = f"Repository: {repository}\n\n{description}".strip()

    # Auto-route planning tasks to Wong
    _title_lower = title.lower()
    _desc_lower = description.lower()
    _is_planning = any(kw in _title_lower or kw in _desc_lower for kw in [
        "plan ", "planning", "write a plan", "design doc", "architecture doc",
        "research ", "investigate", "document ", "documentation", "runbook",
        "readme", "write docs",
    ])

    # Parse assignees
    if assignees.startswith("["):
        try:
            assignee_list = json.loads(assignees)
        except json.JSONDecodeError:
            assignee_list = [a.strip().strip('"[]') for a in assignees.split(",") if a.strip()]
    else:
        assignee_list = [a.strip() for a in assignees.split(",") if a.strip()]
    assignee_list = [a.strip().strip('"\'[]') for a in assignee_list if a.strip()]

    # Auto-route planning/documentation tasks to Wong if no assignee specified
    if _is_planning and not assignee_list:
        assignee_list = ["wong"]

    if len(assignee_list) > 3:
        return f"⚠️ Max 3 agents per task. You listed {len(assignee_list)}: {', '.join(assignee_list)}. Pick the most relevant specialists."

    async with AsyncSessionLocal() as session:
        # Prevent duplicate tasks with the same title
        existing = await session.execute(
            select(Task).where(Task.title.ilike(title))
        )
        if existing.scalar_one_or_none():
            return f"⚠️ Task already exists with title: '{title}'. Use a different title or update the existing task."

        task = Task(
            title=title,
            description=description,
            status=TaskStatus.ASSIGNED if assignee_list else TaskStatus.INBOX,
            priority=TaskPriority(priority) if priority in ["low", "medium", "high", "critical"] else TaskPriority.MEDIUM,
        )
        session.add(task)
        await session.flush()

        assigned_agents = []
        for agent_name in assignee_list:
            result = await session.execute(
                select(AgentModel).where(AgentModel.name.ilike(agent_name))
            )
            agent = result.scalar_one_or_none()
            if agent:
                assignment = TaskAssignment(task_id=task.id, agent_id=agent.id)
                session.add(assignment)
                assigned_agents.append(agent.name)

        activity = Activity(
            type=ActivityType.TASK_CREATED,
            task_id=task.id,
            message=f"Task created: {title}",
        )
        session.add(activity)
        await session.commit()

        repo_label = f"\nRepository: {repository}" if repository else ""
        if assigned_agents:
            return f"✅ Task created: '{title}' (ID: {str(task.id)[:8]})\nAssigned to: {', '.join(assigned_agents)}\nPriority: {priority}{repo_label}"
        else:
            return f"✅ Task created: '{title}' (ID: {str(task.id)[:8]})\nStatus: Inbox (no assignees)\nPriority: {priority}{repo_label}"


@mcp.tool()
@_capture_tool
async def list_tasks(
    status: str = "all",
    limit: int = 10,
) -> str:
    """
    List tasks in Mission Control, optionally filtered by status.
    
    Args:
        status: Filter by status - all, inbox, assigned, in_progress, review, done, blocked
        limit: Maximum number of tasks to return (default 10)
    
    Returns:
        Formatted list of tasks with their status and assignees
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from mission_control.mission_control.core.database import (
        AsyncSessionLocal,
        Task,
        TaskAssignment,
        TaskStatus,
    )

    async with AsyncSessionLocal() as session:
        query = select(Task).options(
            selectinload(Task.assignments).selectinload(TaskAssignment.agent)
        ).order_by(Task.created_at.desc()).limit(limit)

        if status and status.lower() != "all":
            try:
                task_status = TaskStatus(status.lower())
                query = query.where(Task.status == task_status)
            except ValueError:
                pass

        result = await session.execute(query)
        tasks = result.scalars().all()

        if not tasks:
            return f"📋 No tasks found with status: {status}"

        lines = [f"📋 **Tasks ({status})**\n"]
        for task in tasks:
            assignees = [a.agent.name for a in task.assignments if a.agent]
            assignee_str = ", ".join(assignees) if assignees else "Unassigned"
            status_emoji = {
                "inbox": "📥",
                "assigned": "📌",
                "in_progress": "🔄",
                "review": "👀",
                "done": "✅",
                "blocked": "🚫",
            }.get(task.status.value, "📋")
            lines.append(f"{status_emoji} **{task.title}**")
            lines.append(f"   Status: {task.status.value} | Assignees: {assignee_str}")
            if task.description:
                lines.append(f"   {task.description[:100]}...")
            lines.append("")

        return "\n".join(lines)


@mcp.tool()
@_capture_tool
async def list_agents() -> str:
    """
    List all agents in the Mission Control squad with their roles and current status.
    
    Returns:
        Formatted list of agents with their roles and status
    """
    from sqlalchemy import select

    from mission_control.mission_control.core.database import (
        Agent as AgentModel,
    )
    from mission_control.mission_control.core.database import (
        AsyncSessionLocal,
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(AgentModel).order_by(AgentModel.name))
        agents = result.scalars().all()

        if not agents:
            return "🤖 No agents found in the squad"

        lines = ["🤖 **Mission Control Squad**\n"]
        for agent in agents:
            status_emoji = "🟢" if agent.status == "active" else "🔴"
            lines.append(f"{status_emoji} **{agent.name}** - {agent.role}")
            if agent.current_task_id:
                lines.append("   Currently working on a task")
            lines.append("")

        return "\n".join(lines)


@mcp.tool()
@_capture_tool
async def assign_task(
    task_title: str,
    agent_name: str,
) -> str:
    """
    Assign (or add) an agent to an existing task.
    Use this to delegate review, add a second reviewer, or reassign work.

    Args:
        task_title: The title (or partial title) of the task
        agent_name: Name of the agent to assign (e.g., "friday", "shuri")

    Returns:
        Confirmation that the agent was assigned
    """
    from sqlalchemy import select

    from mission_control.mission_control.core.database import (
        Activity,
        ActivityType,
        AsyncSessionLocal,
        Notification,
        Task,
        TaskAssignment,
        TaskStatus,
    )
    from mission_control.mission_control.core.database import (
        Agent as AgentModel,
    )

    async with AsyncSessionLocal() as session:
        # Find task
        result = await session.execute(
            select(Task).where(Task.title.ilike(f"%{task_title}%"))
        )
        tasks = result.scalars().all()

        if not tasks:
            return f"❌ No task found matching: '{task_title}'"
        if len(tasks) > 1:
            titles = "\n".join(f"  • {t.title}" for t in tasks)
            return f"⚠️ Multiple tasks match '{task_title}':\n{titles}\nPlease use a more specific title."

        task = tasks[0]

        # Find agent
        result = await session.execute(
            select(AgentModel).where(AgentModel.name.ilike(agent_name))
        )
        target_agent = result.scalar_one_or_none()
        if not target_agent:
            available = await session.execute(select(AgentModel.name))
            names = [r[0] for r in available.fetchall()]
            return f"❌ Agent '{agent_name}' not found. Available: {', '.join(names)}"

        # Check if already assigned
        existing = await session.execute(
            select(TaskAssignment).where(
                TaskAssignment.task_id == task.id,
                TaskAssignment.agent_id == target_agent.id,
            )
        )
        if existing.scalar_one_or_none():
            return f"ℹ️ {target_agent.name} is already assigned to '{task.title}'"

        # Create assignment
        assignment = TaskAssignment(task_id=task.id, agent_id=target_agent.id)
        session.add(assignment)

        # Transition INBOX → ASSIGNED when first agent is assigned
        if task.status == TaskStatus.INBOX:
            task.status = TaskStatus.ASSIGNED

        # Notify the agent
        notification = Notification(
            mentioned_agent_id=target_agent.id,
            content=f"You have been assigned to: {task.title} (status: {task.status.value})",
        )
        session.add(notification)

        # Log activity
        activity = Activity(
            type=ActivityType.TASK_ASSIGNED,
            agent_id=target_agent.id,
            task_id=task.id,
            message=f"{target_agent.name} assigned to task",
        )
        session.add(activity)

        await session.commit()
        return f"✅ {target_agent.name} assigned to '{task.title}' (status: {task.status.value})"


@mcp.tool()
@_capture_tool
async def delegate_to_agent(
    agent_name: str,
    message: str,
) -> str:
    """
    Send a message or delegate work to another agent in the squad.
    Creates a notification that the target agent will receive in their next heartbeat.
    
    Args:
        agent_name: Name of the agent to delegate to (e.g., "friday", "shuri")
        message: The message or task details to send
    
    Returns:
        Confirmation that the delegation was sent
    """
    from sqlalchemy import select

    from mission_control.mission_control.core.database import (
        Agent as AgentModel,
    )
    from mission_control.mission_control.core.database import (
        AsyncSessionLocal,
        Notification,
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AgentModel).where(AgentModel.name.ilike(agent_name))
        )
        target_agent = result.scalar_one_or_none()

        if not target_agent:
            available = await session.execute(select(AgentModel.name))
            names = [r[0] for r in available.fetchall()]
            return f"❌ Agent '{agent_name}' not found. Available: {', '.join(names)}"

        # Create notification directly (Message model requires task_id/from_agent_id)
        notification = Notification(
            mentioned_agent_id=target_agent.id,
            content=message,
        )
        session.add(notification)
        await session.commit()

        return f"📨 Message sent to {target_agent.name}. They will receive it in their next heartbeat."


@mcp.tool()
@_capture_tool
async def update_task_status(
    task_title: str,
    new_status: str,
) -> str:
    """
    Update the status of an existing task.
    
    Args:
        task_title: The title (or partial title) of the task to update
        new_status: New status - inbox, assigned, in_progress, review, done, blocked
    
    Returns:
        Confirmation of the status change
    """
    from sqlalchemy import select

    from mission_control.mission_control.core.database import (
        Activity,
        ActivityType,
        AsyncSessionLocal,
        Task,
        TaskStatus,
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.title.ilike(f"%{task_title}%"))
        )
        tasks = result.scalars().all()

        if not tasks:
            return f"❌ No task found matching: '{task_title}'"

        if len(tasks) > 1:
            titles = "\n".join(f"  • {t.title}" for t in tasks)
            return f"⚠️ Multiple tasks match '{task_title}':\n{titles}\nPlease use a more specific title."

        task = tasks[0]
        try:
            old_status = task.status
            new = TaskStatus(new_status.lower())

            if old_status == new:
                return f"ℹ️ Task '{task.title}' is already {old_status.value}. No change made."

            task.status = new

            activity = Activity(
                type=ActivityType.TASK_STATUS_CHANGED,
                task_id=task.id,
                message=f"Status: {old_status.value} → {task.status.value}",
            )
            session.add(activity)
            await session.commit()

            return f"✅ Task '{task.title}' status updated: {old_status.value} → {task.status.value}"
        except ValueError:
            return f"❌ Invalid status '{new_status}'. Valid: inbox, assigned, in_progress, review, done, blocked"


@mcp.tool()
@_capture_tool
async def get_my_tasks(agent_name: str = "jarvis") -> str:
    """
    Get tasks assigned to a specific agent.
    
    Args:
        agent_name: Name of the agent (default: jarvis)
    
    Returns:
        List of tasks assigned to the agent
    """
    from sqlalchemy import select

    from mission_control.mission_control.core.database import (
        Agent as AgentModel,
    )
    from mission_control.mission_control.core.database import (
        AsyncSessionLocal,
        Task,
        TaskAssignment,
    )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AgentModel).where(AgentModel.name.ilike(agent_name))
        )
        agent = result.scalar_one_or_none()

        if not agent:
            return f"❌ Agent '{agent_name}' not found"

        result = await session.execute(
            select(Task)
            .join(TaskAssignment)
            .where(TaskAssignment.agent_id == agent.id)
            .order_by(Task.priority.desc(), Task.created_at.desc())
        )
        tasks = result.scalars().all()

        if not tasks:
            return f"📋 No tasks assigned to {agent.name}"

        lines = [f"📋 **Tasks for {agent.name}**\n"]
        for task in tasks:
            status_emoji = {
                "inbox": "📥", "assigned": "📌", "in_progress": "🔄",
                "review": "👀", "done": "✅", "blocked": "🚫",
            }.get(task.status.value, "📋")
            lines.append(f"{status_emoji} **{task.title}** - {task.status.value}")
            if task.description:
                lines.append(f"   {task.description[:80]}...")

        return "\n".join(lines)


@mcp.tool()
@_capture_tool
async def delete_task(task_title: str) -> str:
    """
    Delete a task from Mission Control.
    
    Args:
        task_title: The title (or partial title) of the task to delete.
                    If multiple tasks match, all matches are deleted.
    
    Returns:
        Confirmation of the deletion
    """
    from sqlalchemy import delete, select

    from mission_control.mission_control.core.database import (
        Activity,
        AsyncSessionLocal,
        Task,
        TaskAssignment,
    )

    async with AsyncSessionLocal() as session:
        # Find all matching tasks
        result = await session.execute(
            select(Task).where(Task.title.ilike(f"%{task_title}%"))
        )
        tasks = result.scalars().all()

        if not tasks:
            return f"❌ No task found matching: '{task_title}'"

        deleted_titles = []
        for task in tasks:
            await session.execute(
                delete(TaskAssignment).where(TaskAssignment.task_id == task.id)
            )
            await session.execute(
                delete(Activity).where(Activity.task_id == task.id)
            )
            deleted_titles.append(task.title)
            await session.delete(task)

        await session.commit()

        if len(deleted_titles) == 1:
            return f"🗑️ Task deleted: '{deleted_titles[0]}'"
        return f"🗑️ Deleted {len(deleted_titles)} tasks:\n" + "\n".join(f"  • {t}" for t in deleted_titles)


@mcp.tool()
@_capture_tool
async def create_document(
    title: str,
    content: str,
    doc_type: str = "deliverable",
    task_title: str = "",
) -> str:
    """
    Create a document in Mission Control, optionally linked to a task.

    Args:
        title: The document title (required)
        content: The document content/body (required)
        doc_type: Document type - deliverable, research, protocol, or runbook
        task_title: Optional task title to link this document to

    Returns:
        Confirmation message with document details
    """
    from sqlalchemy import select

    from mission_control.mission_control.core.database import (
        AsyncSessionLocal,
        Document,
        Task,
    )

    doc_type = doc_type or "deliverable"
    task_title = task_title or ""

    async with AsyncSessionLocal() as session:
        task_id = None
        if task_title:
            result = await session.execute(
                select(Task).where(Task.title.ilike(f"%{task_title}%"))
            )
            task = result.scalar_one_or_none()
            if task:
                task_id = task.id

        doc = Document(
            title=title,
            content=content,
            type=doc_type,
            task_id=task_id,
        )
        session.add(doc)
        await session.commit()

        linked = f" (linked to task '{task_title}')" if task_id else ""
        return f"📄 Document created: '{title}' [type: {doc_type}]{linked} (ID: {str(doc.id)[:8]})"


@mcp.tool()
@_capture_tool
async def list_documents(
    doc_type: str = "all",
    limit: int = 10,
) -> str:
    """
    List documents in Mission Control, optionally filtered by type.

    Args:
        doc_type: Filter by type - all, deliverable, research, protocol, runbook
        limit: Maximum number of documents to return (default 10)

    Returns:
        Formatted list of documents
    """
    from sqlalchemy import select

    from mission_control.mission_control.core.database import (
        AsyncSessionLocal,
        Document,
    )

    async with AsyncSessionLocal() as session:
        query = select(Document).order_by(Document.created_at.desc()).limit(limit)

        if doc_type and doc_type.lower() != "all":
            query = query.where(Document.type == doc_type.lower())

        result = await session.execute(query)
        docs = result.scalars().all()

        if not docs:
            return f"📄 No documents found with type: {doc_type}"

        lines = [f"📄 **Documents ({doc_type})**\n"]
        for doc in docs:
            type_emoji = {
                "deliverable": "📦",
                "research": "🔬",
                "protocol": "📋",
                "runbook": "📖",
            }.get(doc.type or "", "📄")
            lines.append(f"{type_emoji} **{doc.title}** [type: {doc.type or 'unknown'}]")
            if doc.content:
                lines.append(f"   {doc.content[:100]}...")
            lines.append("")

        return "\n".join(lines)


# ============================================================================
# Server Entry Point
# ============================================================================

if __name__ == "__main__":
    transport = MCP_TRANSPORT
    logger.info(f"Starting Mission Control MCP Server (transport={transport}, port={MCP_PORT})...")
    mcp.run(transport=transport)
