"""
Tools for Mission Control agents.

These tools allow agents to create tasks, delegate work, and communicate with each other.
"""

from datetime import datetime, timezone

import structlog
from agno.tools import tool
from sqlalchemy import select

from mission_control.mission_control.core.database import (
    Activity,
    ActivityType,
    AsyncSessionLocal,
    Document,
    Notification,
    Task,
    TaskAssignment,
    TaskPriority,
    TaskStatus,
)
from mission_control.mission_control.core.database import (
    Agent as AgentModel,
)

logger = structlog.get_logger()


@tool(
    name="create_task",
    description="Create a new task and optionally assign it to agents. Use this when the user wants to create work items, assign tasks, or delegate work to team members.",
)
async def create_task(
    title: str,
    description: str = "",
    assignees: str = "",
    priority: str = "medium",
    repository: str = "",
    source_branch: str = "",
) -> str:
    """
    Create a new task in Mission Control.

    Args:
        title: The task title (required)
        description: Detailed description of the task
        assignees: Comma-separated agent names to assign (e.g., "friday,shuri")
        priority: Task priority - low, medium, high, or critical
        repository: Target GitHub repo (e.g., "Amaresh/apiblender"). Stored in mission_config.
        source_branch: Base branch to branch from (default: repo default). Stored in mission_config.

    Returns:
        Confirmation message with task ID
    """
    import json

    # Handle None values from LLM
    assignees = assignees or ""
    priority = priority or "medium"
    description = description or ""

    # Handle both JSON array format and comma-separated format
    if assignees.startswith("["):
        try:
            assignee_list = json.loads(assignees)
        except json.JSONDecodeError:
            assignee_list = [a.strip().strip('"[]') for a in assignees.split(",") if a.strip()]
    else:
        assignee_list = [a.strip() for a in assignees.split(",") if a.strip()]

    # Clean up any remaining quotes or brackets
    assignee_list = [a.strip().strip('"\'[]') for a in assignee_list if a.strip()]

    if len(assignee_list) > 3:
        return f"⚠️ Max 3 agents per task. You listed {len(assignee_list)}: {', '.join(assignee_list)}. Pick the most relevant specialists."

    async with AsyncSessionLocal() as session:
        # Prevent duplicate tasks with the same title
        existing = await session.execute(
            select(Task).where(Task.title.ilike(title))
        )
        if existing.scalar_one_or_none():
            return f"⚠️ Task already exists with title: '{title}'. Use a different title or update the existing task."

        # Build mission_config from explicit params + description fallback
        mission_config = {}
        repo = (repository or "").strip()
        if not repo and description and "Repository:" in description:
            import re
            m = re.search(r"Repository:\s*(\S+)", description)
            if m:
                repo = m.group(1).strip()
        if repo:
            mission_config["repository"] = repo
        branch = (source_branch or "").strip()
        if not branch and description and "Base Branch:" in description:
            import re
            m = re.search(r"Base Branch:\s*(\S+)", description)
            if m:
                branch = m.group(1).strip()
        if branch:
            mission_config["source_branch"] = branch

        # Create task
        task = Task(
            title=title,
            description=description,
            status=TaskStatus.ASSIGNED if assignee_list else TaskStatus.INBOX,
            priority=TaskPriority(priority) if priority in ["low", "medium", "high", "critical"] else TaskPriority.MEDIUM,
            mission_config=mission_config,
        )
        session.add(task)
        await session.flush()

        # Log activity
        activity = Activity(
            type=ActivityType.TASK_CREATED,
            task_id=task.id,
            message=f"Created task: {title}",
        )
        session.add(activity)

        assigned_names = []
        # Assign to agents
        for assignee_name in assignee_list:
            stmt = select(AgentModel).where(
                AgentModel.name.ilike(assignee_name)
            )
            result = await session.execute(stmt)
            agent = result.scalar_one_or_none()

            if agent:
                assignment = TaskAssignment(
                    task_id=task.id,
                    agent_id=agent.id,
                )
                session.add(assignment)
                assigned_names.append(agent.name)

                # Create notification
                notif = Notification(
                    mentioned_agent_id=agent.id,
                    content=f"You have been assigned: {title}",
                )
                session.add(notif)

        await session.commit()

        logger.info(
            "Task created via tool",
            task_id=str(task.id),
            title=title,
            assignees=assigned_names,
        )

        if assigned_names:
            return f"✅ Created task '{title}' (ID: {str(task.id)[:8]}) and assigned to: {', '.join(assigned_names)}"
        else:
            return f"✅ Created task '{title}' (ID: {str(task.id)[:8]}) in inbox (no assignees)"


@tool(
    name="list_tasks",
    description="List tasks in Mission Control. Use this to see what tasks exist, their status, and who they're assigned to.",
)
async def list_tasks(
    status: str = "all",
    limit: int = 10,
) -> str:
    """
    List tasks from Mission Control.

    Args:
        status: Filter by status - all, inbox, assigned, in_progress, review, done, blocked
        limit: Maximum number of tasks to return

    Returns:
        Formatted list of tasks
    """
    async with AsyncSessionLocal() as session:
        stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)

        if status != "all":
            try:
                status_enum = TaskStatus(status.upper())
                stmt = stmt.where(Task.status == status_enum)
            except ValueError:
                pass

        result = await session.execute(stmt)
        tasks = result.scalars().all()

        if not tasks:
            return "No tasks found."

        lines = ["📋 **Tasks:**\n"]
        for t in tasks:
            status_icon = {
                TaskStatus.INBOX: "📥",
                TaskStatus.ASSIGNED: "📌",
                TaskStatus.IN_PROGRESS: "🔄",
                TaskStatus.REVIEW: "👀",
                TaskStatus.DONE: "✅",
                TaskStatus.BLOCKED: "🚫",
            }.get(t.status, "❓")

            lines.append(f"{status_icon} **{t.title}** [{t.status.value}] - {t.priority.value}")

        return "\n".join(lines)


@tool(
    name="list_agents",
    description="List all agents in the Mission Control squad with their roles and current status.",
)
async def list_agents() -> str:
    """
    List all agents in Mission Control.

    Returns:
        Formatted list of agents with their roles
    """
    async with AsyncSessionLocal() as session:
        stmt = select(AgentModel).order_by(AgentModel.name)
        result = await session.execute(stmt)
        agents = result.scalars().all()

        if not agents:
            return "No agents found."

        lines = ["🤖 **Agent Squad:**\n"]
        for a in agents:
            last_hb = a.last_heartbeat.strftime("%H:%M") if a.last_heartbeat else "never"
            lines.append(f"• **{a.name}** - {a.role} (last heartbeat: {last_hb})")

        return "\n".join(lines)


@tool(
    name="assign_task",
    description="Assign an agent to an existing task. Use to add reviewers, delegate work to specialists, or reassign tasks.",
)
async def assign_task(
    task_title: str,
    agent_name: str,
) -> str:
    """
    Assign (or add) an agent to an existing task.

    Args:
        task_title: Title (or part of title) of the task
        agent_name: Name of the agent to assign (e.g., "friday", "shuri")

    Returns:
        Confirmation of the assignment
    """
    async with AsyncSessionLocal() as session:
        stmt = select(Task).where(Task.title.ilike(f"%{task_title}%"))
        result = await session.execute(stmt)
        tasks = result.scalars().all()

        if not tasks:
            return f"❌ No task found matching '{task_title}'"
        if len(tasks) > 1:
            titles = "\n".join(f"  • {t.title}" for t in tasks)
            return f"⚠️ Multiple tasks match. Be more specific:\n{titles}"

        task = tasks[0]

        stmt = select(AgentModel).where(AgentModel.name.ilike(agent_name))
        result = await session.execute(stmt)
        agent = result.scalar_one_or_none()
        if not agent:
            return f"❌ Agent '{agent_name}' not found."

        # Check existing
        from mission_control.mission_control.core.database import TaskAssignment
        existing = await session.execute(
            select(TaskAssignment).where(
                TaskAssignment.task_id == task.id,
                TaskAssignment.agent_id == agent.id,
            )
        )
        if existing.scalar_one_or_none():
            return f"ℹ️ {agent.name} is already assigned to '{task.title}'"

        assignment = TaskAssignment(task_id=task.id, agent_id=agent.id)
        session.add(assignment)

        notif = Notification(
            mentioned_agent_id=agent.id,
            content=f"You have been assigned to: {task.title} (status: {task.status.value})",
        )
        session.add(notif)

        activity = Activity(
            type=ActivityType.TASK_ASSIGNED,
            agent_id=agent.id,
            task_id=task.id,
            message=f"{agent.name} assigned to task",
        )
        session.add(activity)

        await session.commit()
        return f"✅ {agent.name} assigned to '{task.title}' (status: {task.status.value})"


@tool(
    name="delegate_to_agent",
    description="Delegate a message or request to another agent. Use this when work should be handled by a specialist (e.g., Friday for coding, Shuri for testing).",
)
async def delegate_to_agent(
    agent_name: str,
    message: str,
) -> str:
    """
    Delegate work to another agent by creating a notification.

    Args:
        agent_name: Name of the agent to delegate to (e.g., "friday", "shuri")
        message: The message or request to send to the agent

    Returns:
        Confirmation that the delegation was created
    """
    async with AsyncSessionLocal() as session:
        stmt = select(AgentModel).where(
            AgentModel.name.ilike(agent_name)
        )
        result = await session.execute(stmt)
        agent = result.scalar_one_or_none()

        if not agent:
            return f"❌ Agent '{agent_name}' not found. Available agents: Jarvis, Friday, Vision, Wong, Shuri, Fury, Pepper"

        # Create notification
        notif = Notification(
            mentioned_agent_id=agent.id,
            content=message,
        )
        session.add(notif)

        # Log activity
        activity = Activity(
            type=ActivityType.MESSAGE_SENT,
            agent_id=agent.id,
            message=f"Delegated to {agent.name}: {message[:100]}",
        )
        session.add(activity)

        await session.commit()

        logger.info(
            "Delegated to agent",
            target_agent=agent.name,
            message_preview=message[:50],
        )

        return f"✅ Delegated to {agent.name}. They will pick this up on their next heartbeat."


@tool(
    name="update_task_status",
    description="Update the status of a task. Use this to move tasks through the workflow (e.g., start work, submit for review, mark done).",
)
async def update_task_status(
    task_title: str,
    new_status: str,
) -> str:
    """
    Update a task's status.

    Args:
        task_title: Title (or part of title) of the task to update
        new_status: New status - inbox, assigned, in_progress, review, done, blocked

    Returns:
        Confirmation of the status change
    """
    async with AsyncSessionLocal() as session:
        stmt = select(Task).where(
            Task.title.ilike(f"%{task_title}%")
        ).order_by(Task.created_at.desc()).limit(1)

        result = await session.execute(stmt)
        task = result.scalar_one_or_none()

        if not task:
            return f"❌ No task found matching '{task_title}'"

        try:
            old_status = task.status
            new = TaskStatus(new_status.upper())

            if old_status == new:
                return f"ℹ️ Task '{task.title}' is already {old_status.value}. No change made."

            # Block IN_PROGRESS → REVIEW unless an open PR exists in the target repo
            if old_status == TaskStatus.IN_PROGRESS and new == TaskStatus.REVIEW:
                from mission_control.mission_control.core.pr_check import (
                    extract_target_repo,
                    has_open_pr,
                )
                target_repo = extract_target_repo(task.description)
                if target_repo:
                    head_prefix = ""
                    assignment = await session.execute(
                        select(TaskAssignment).where(TaskAssignment.task_id == task.id)
                    )
                    ta = assignment.scalar_one_or_none()
                    if ta:
                        agent_rec = await session.execute(
                            select(AgentModel).where(AgentModel.id == ta.agent_id)
                        )
                        ar = agent_rec.scalar_one_or_none()
                        if ar:
                            head_prefix = f"{ar.name.lower()}/"
                    pr_found, _ = await has_open_pr(target_repo, head_prefix)
                    if not pr_found:
                        return (
                            f"⚠️ Cannot move to REVIEW — no open PR found in {target_repo} "
                            f"with head branch starting with '{head_prefix}'. "
                            f"Open a PR first, then try again."
                        )

            task.status = new
            task.updated_at = datetime.now(timezone.utc)

            # Log activity
            activity = Activity(
                type=ActivityType.TASK_STATUS_CHANGED,
                task_id=task.id,
                message=f"Status changed: {old_status.value} → {task.status.value}",
            )
            session.add(activity)

            await session.commit()

            return f"✅ Task '{task.title}' status updated: {old_status.value} → {task.status.value}"
        except ValueError:
            return f"❌ Invalid status '{new_status}'. Valid options: inbox, assigned, in_progress, review, done, blocked"


@tool(
    name="create_document",
    description="Create a document in Mission Control, optionally linked to a task. Use for deliverables, research notes, protocols, or runbooks.",
)
async def create_document(
    title: str,
    content: str,
    doc_type: str = "deliverable",
    task_title: str = "",
) -> str:
    """
    Create a document in Mission Control.

    Args:
        title: The document title (required)
        content: The document content/body (required)
        doc_type: Document type - deliverable, research, protocol, or runbook
        task_title: Optional task title to link this document to

    Returns:
        Confirmation message with document details
    """
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

        logger.info(
            "Document created via tool",
            doc_id=str(doc.id),
            title=title,
            doc_type=doc_type,
        )

        linked = f" (linked to task '{task_title}')" if task_id else ""
        return f"📄 Document created: '{title}' [type: {doc_type}]{linked} (ID: {str(doc.id)[:8]})"


@tool(
    name="list_documents",
    description="List documents in Mission Control. Use this to see existing deliverables, research notes, protocols, or runbooks.",
)
async def list_documents(
    doc_type: str = "all",
    limit: int = 10,
) -> str:
    """
    List documents from Mission Control.

    Args:
        doc_type: Filter by type - all, deliverable, research, protocol, runbook
        limit: Maximum number of documents to return

    Returns:
        Formatted list of documents
    """
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


# Export all tools as a list for easy registration
MISSION_CONTROL_TOOLS = [
    create_task,
    list_tasks,
    list_agents,
    assign_task,
    delegate_to_agent,
    update_task_status,
    create_document,
    list_documents,
]
