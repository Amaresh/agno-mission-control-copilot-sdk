"""
Base Agent class for Mission Control.

Uses Agno framework patterns:
- PostgresDb for session persistence
- Singleton agent instances (created once, reused)
- Session IDs for user/conversation tracking
- Learning for memory across sessions
"""

import asyncio
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import structlog
from agno.agent import Agent as AgnoAgent
from agno.db.postgres import PostgresDb
from agno.models.groq import Groq
from agno.models.ollama import Ollama

from agents.config import settings

from .copilot_model import CopilotModel

logger = structlog.get_logger()

# Shared database connection for all agents (Agno pattern)
_agno_db: Optional[PostgresDb] = None

def get_agno_db() -> PostgresDb:
    """Get shared PostgresDb instance for Agno agents."""
    global _agno_db
    if _agno_db is None:
        # Convert async URL to psycopg format for Agno
        db_url = settings.database_url.replace(
            "postgresql://", "postgresql+psycopg://"
        )
        _agno_db = PostgresDb(
            db_url=db_url,
            session_table="agno_sessions",
        )
        # Mask password in log output
        safe_url = db_url.split("@")[-1] if "@" in db_url else db_url[:50]
        logger.info("Initialized Agno PostgresDb", db_host=safe_url)
    return _agno_db


class BaseAgent(ABC):
    """
    Base class for all Mission Control agents.

    Provides:
    - LLM initialization (Ollama primary, Groq fallback)
    - Memory management (SOUL, WORKING, daily notes)
    - MCP tool integration
    - Heartbeat handling
    """

    def __init__(
        self,
        name: str,
        role: str,
        session_key: str,
        mcp_servers: Optional[list[str]] = None,
        heartbeat_offset: int = 0,
        level: str = "specialist",
    ):
        self.name = name
        self.role = role
        self.session_key = session_key
        self.mcp_servers = mcp_servers or []
        self.heartbeat_offset = heartbeat_offset
        self.level = level

        # Paths
        self.base_path = Path(f"agents/squad/{name.lower()}")
        self.soul_path = self.base_path / "SOUL.md"
        self.working_path = self.base_path / "WORKING.md"
        self.memory_path = self.base_path / "MEMORY.md"
        self.daily_path = self.base_path / "daily"

        # Ensure directories exist
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.daily_path.mkdir(parents=True, exist_ok=True)

        # Agent instance (lazy loaded)
        self._agent: Optional[AgnoAgent] = None
        self._mcp_tools: list = []

        self.logger = logger.bind(agent=name)

    @property
    def soul(self) -> str:
        """Load agent's SOUL (identity)."""
        if self.soul_path.exists():
            return self.soul_path.read_text()
        return self._default_soul()

    @property
    def working_memory(self) -> str:
        """Load current working memory."""
        if self.working_path.exists():
            return self.working_path.read_text()
        return "# WORKING.md\n\nNo current task."

    def update_working_memory(self, content: str):
        """Update working memory file."""
        self.working_path.write_text(content)
        self.logger.info("Updated working memory")

    def append_daily_note(self, note: str):
        """Append to today's daily notes."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_file = self.daily_path / f"{today}.md"

        timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
        entry = f"\n## {timestamp}\n{note}\n"

        if daily_file.exists():
            with open(daily_file, "a") as f:
                f.write(entry)
        else:
            daily_file.write_text(f"# Daily Notes - {today}\n{entry}")

    def _default_soul(self) -> str:
        """Default SOUL template."""
        return f"""# SOUL.md - {self.name}

**Name:** {self.name}
**Role:** {self.role}

## Personality
A helpful and efficient AI agent.

## What You're Good At
- Following instructions
- Completing assigned tasks
- Collaborating with other agents

## What You Care About
- Quality work
- Clear communication
- Team success
"""

    async def _init_agent(self) -> AgnoAgent:
        """Initialize the Agno agent with LLM, tools, and database."""
        from agents.mission_control.mcp.manager import MCPManager
        from agents.mission_control.tools import MISSION_CONTROL_TOOLS

        # Get MCP tools (external integrations)
        # Note: MCP tools require async initialization
        mcp_manager = MCPManager()
        self._mcp_tools = []

        if self.mcp_servers:
            try:
                mcp_tools_list = await mcp_manager.get_tools_for_agent(self.mcp_servers)
                for mcp_tool in mcp_tools_list:
                    try:
                        # MCPTools requires connect() to be called
                        await mcp_tool.connect()
                        self._mcp_tools.append(mcp_tool)
                        self.logger.info("Connected MCP tool", server=mcp_tool.tool_name_prefix)
                    except Exception as e:
                        self.logger.warning("Failed to connect MCP tool", error=str(e))
            except Exception as e:
                self.logger.warning("MCP tools failed to load, continuing without", error=str(e))

        # Combine MCP tools with Mission Control tools (task creation, delegation, etc.)
        all_tools = list(MISSION_CONTROL_TOOLS) + self._mcp_tools

        # Build instructions
        instructions = self._build_instructions()

        # Model priority: Copilot SDK (GPT-4.1) > Ollama > Groq
        model = None

        # Try Copilot SDK first (premium model via GitHub)
        if settings.use_copilot_sdk:
            try:
                model = CopilotModel(id=settings.copilot_model)

                # Configure MCP servers for Copilot SDK native tool support
                mcp_servers = self._build_copilot_mcp_config()
                if mcp_servers:
                    model.set_mcp_servers(mcp_servers)
                    self.logger.info("Configured MCP servers for Copilot", num_servers=len(mcp_servers))

                self.logger.info("Using Copilot SDK model", model=settings.copilot_model)
            except Exception as e:
                self.logger.warning("Copilot SDK unavailable", error=str(e))
                model = None

        # Fallback to Ollama
        if model is None:
            try:
                model = Ollama(
                    id=settings.default_model,
                    host=settings.ollama_host,
                )
                self.logger.info("Using Ollama model", model=settings.default_model)
            except Exception as e:
                self.logger.warning("Ollama unavailable, falling back to Groq", error=str(e))
                if settings.groq_api_key:
                    model = Groq(
                        id=settings.fallback_model,
                        api_key=settings.groq_api_key,
                    )
                    self.logger.info("Using Groq model", model=settings.fallback_model)
                else:
                    raise RuntimeError("No LLM available: Copilot/Ollama failed and no Groq API key")

        # Get shared database for session persistence (Agno pattern)
        db = get_agno_db()

        agent = AgnoAgent(
            name=self.name,
            model=model,
            tools=all_tools,
            instructions=instructions,
            db=db,  # Session persistence
            # History & Context
            add_history_to_context=True,  # Remember conversation history
            num_history_runs=5,  # Include last 5 exchanges
            # Learning & Memory
            learning=True,  # Enable learning from interactions
            add_learnings_to_context=True,  # Use learnings in responses
            enable_agentic_memory=True,  # Agentic memory management
            enable_user_memories=True,  # Remember user-specific info
            add_memories_to_context=True,  # Include memories in context
            # Session Management
            enable_session_summaries=True,  # Summarize long sessions
            add_session_summary_to_context=True,  # Use summaries
            # Output
            markdown=True,
        )

        return agent

    def _build_copilot_mcp_config(self) -> Dict[str, Any]:
        """Build MCP server config for Copilot SDK sessions.
        
        The Copilot SDK handles tool calls internally via its MCP servers.
        Agno MCPTools are NOT used for tool execution with CopilotModel —
        the SDK discovers and calls tools from these MCP servers directly.
        """
        mcp_servers = {}

        # Mission Control MCP — read-only for specialists; lead agents also
        # get update_task_status so they can move tasks out of REVIEW.
        mcp_port = int(os.environ.get("MCP_PORT", "8001"))
        mc_tools = ["list_tasks", "list_agents", "get_my_tasks", "list_documents"]
        if self.level == "lead":
            mc_tools.extend(["update_task_status", "create_task", "assign_task"])
        mcp_servers["mission-control"] = {
            "type": "sse",
            "url": f"http://127.0.0.1:{mcp_port}/sse",
            "tools": mc_tools,
        }

        # GitHub MCP — spawn @modelcontextprotocol/server-github via wrapper script.
        # Only added for agents with "github" in their mcp_servers list.
        #
        # IMPORTANT: Agents must NEVER modify the mission-control repo itself.
        # We restrict GitHub MCP to read-only + issue/PR tools (no file writes).
        from agents.config import settings
        if "github" in self.mcp_servers and settings.github_token:
            wrapper_path = self._ensure_github_mcp_wrapper(settings.github_token)
            github_tools = [
                # Read-only
                "get_file_contents", "search_code", "search_repositories",
                "search_issues", "list_commits", "get_commit",
                "list_branches",
                # Issues & PRs (read + create/comment — no repo file writes)
                "list_issues", "get_issue", "create_issue", "add_issue_comment",
                "list_pull_requests", "get_pull_request", "get_pull_request_diff",
                "create_pull_request", "add_pull_request_review_comment",
                # Explicitly excluded: create_or_update_file, push_files,
                # create_repository, delete_file, fork_repository, create_branch
            ]
            mcp_servers["github"] = {
                "type": "local",
                "command": wrapper_path,
                "args": [],
                "tools": github_tools,
            }

        # DigitalOcean MCP — @digitalocean/mcp via wrapper script.
        # Only added for agents with "digitalocean" in their mcp_servers list.
        if "digitalocean" in self.mcp_servers:
            if settings.do_api_token:
                do_wrapper = self._ensure_do_mcp_wrapper(settings.do_api_token)
                mcp_servers["digitalocean"] = {
                    "type": "local",
                    "command": do_wrapper,
                    "args": [],
                }

        return mcp_servers

    @staticmethod
    def _ensure_github_mcp_wrapper(token: str) -> str:
        """Create/update the wrapper script that launches GitHub MCP with the token."""
        import stat
        wrapper_path = "/tmp/github-mcp-wrapper.sh"
        script = f"""#!/bin/bash
export GITHUB_PERSONAL_ACCESS_TOKEN="{token}"
exec npx -y @modelcontextprotocol/server-github "$@"
"""
        # Only rewrite if content changed (avoid race conditions)
        try:
            with open(wrapper_path) as f:
                if f.read() == script:
                    return wrapper_path
        except FileNotFoundError:
            pass
        with open(wrapper_path, "w") as f:
            f.write(script)
        os.chmod(wrapper_path, stat.S_IRWXU)  # 0o700 — owner only
        return wrapper_path

    @staticmethod
    def _ensure_do_mcp_wrapper(token: str) -> str:
        """Create/update the wrapper script that launches DigitalOcean MCP with the token."""
        import stat
        wrapper_path = "/tmp/do-mcp-wrapper.sh"
        script = f"""#!/bin/bash
exec npx -y @digitalocean/mcp -digitalocean-api-token "{token}" "$@"
"""
        try:
            with open(wrapper_path) as f:
                if f.read() == script:
                    return wrapper_path
        except FileNotFoundError:
            pass
        with open(wrapper_path, "w") as f:
            f.write(script)
        os.chmod(wrapper_path, stat.S_IRWXU)  # 0o700 — owner only
        return wrapper_path

    def _build_instructions(self) -> str:
        """Build agent instructions from SOUL, AGENTS.md, HEARTBEAT.md, and context."""
        import os

        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        agents_md = ""
        agents_md_path = os.path.join(base_dir, "AGENTS.md")
        if os.path.exists(agents_md_path):
            with open(agents_md_path, "r") as f:
                agents_md = f.read()

        heartbeat_md = ""
        heartbeat_md_path = os.path.join(base_dir, "HEARTBEAT.md")
        if os.path.exists(heartbeat_md_path):
            with open(heartbeat_md_path, "r") as f:
                heartbeat_md = f.read()

        return f"""## ENVIRONMENT CONSTRAINTS (READ FIRST — MANDATORY)
You are running as a headless agent WITHOUT local filesystem access.
- Do NOT use bash, shell, terminal, or filesystem tools. They are disabled.
- Do NOT write, read, or create local files. You have no working directory.
- Do NOT invoke ANY Copilot skills — brainstorming, executing-plans, writing-plans, make-skill-template, prd, etc. are ALL FORBIDDEN. If a skill prompt fires, IGNORE it completely.
- The ONLY way to create files is via the GitHub MCP server tools: `create_branch`, `create_or_update_file`, `create_pull_request`.
- When given a task, call these GitHub tools immediately. Do not plan or explain first.
- Do NOT call `update_task_status` — the orchestrator handles status transitions.
- NEVER create plan documents, outlines, breakdown .md files, or task decompositions. Write REAL implementation code (.py, .js, .ts, .yaml, etc.), tests, or technical documentation that a developer would ship.

---

{agents_md}

---

## Your Identity
{self.soul}

---

## Current Context

{self.working_memory}

---

{heartbeat_md}

## Available MCP Servers
{', '.join(self.mcp_servers) if self.mcp_servers else 'None configured'}
"""

    async def get_agent(self) -> AgnoAgent:
        """Get or create the agent instance."""
        if self._agent is None:
            self._agent = await self._init_agent()
        return self._agent

    def set_repo_scope(self, repo: Optional[str]) -> None:
        """Set (or clear) allowed-repo constraint on all RepoScopedMCPTools."""
        from agents.mission_control.mcp.repo_scoped import RepoScopedMCPTools

        for t in self._mcp_tools:
            if isinstance(t, RepoScopedMCPTools):
                t.set_allowed_repo(repo)

    async def run(
        self,
        message: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """
        Run the agent with a message and return response.
        
        Enriches each request with relevant learning patterns from the DB
        so the LLM can avoid past mistakes and reuse proven strategies.
        
        Args:
            message: The user message to process
            user_id: Optional user identifier for personalization
            session_id: Optional session identifier for conversation continuity
        """
        agent = await self.get_agent()

        self.logger.info(
            "Running agent",
            message_preview=message[:100],
            user_id=user_id,
            session_id=session_id,
        )
        self.append_daily_note(f"Received: {message[:200]}...")

        # Enrich message with relevant learning patterns from the DB
        enriched_message = await self._enrich_with_learnings(message)

        try:
            # Set user context on CopilotModel for session management
            # This ensures Copilot sessions are cached per-user for context continuity
            if hasattr(agent.model, 'set_user_context') and user_id:
                agent.model.set_user_context(user_id)

            # Pass user_id and session_id to Agno for session tracking
            response = await agent.arun(
                enriched_message,
                user_id=user_id,
                session_id=session_id or self.session_key,
            )
            result = response.content if hasattr(response, 'content') else str(response)

            self.append_daily_note(f"Responded: {result[:200]}...")
            self.logger.info("Agent completed", response_preview=result[:100])

            return result
        except Exception as e:
            self.logger.error("Agent error", error=str(e))
            self.append_daily_note(f"ERROR: {str(e)}")

            # Capture for learning
            await self._capture_error(message, e)
            raise

    async def _enrich_with_learnings(self, message: str) -> str:
        """Prepend relevant learning patterns to the message.

        Queries the learning_patterns table for patterns whose trigger_text
        matches keywords in the message, then formats them as context the
        LLM can use.  Fails silently — learning recall must never block work.
        """
        try:
            from agents.mission_control.learning.capture import (
                format_patterns_for_context,
                get_relevant_patterns,
            )
            patterns = await get_relevant_patterns(query=message, limit=3)
            if patterns:
                context = format_patterns_for_context(patterns)
                self.logger.debug(
                    "Injected learning patterns",
                    num_patterns=len(patterns),
                    context_len=len(context),
                )
                return f"{context}\n\n{message}"
        except Exception as e:
            self.logger.warning("Learning recall failed (non-fatal)", error=str(e))
        return message

    async def _capture_error(self, message: str, error: Exception):
        """Capture error for learning system."""
        from agents.mission_control.learning.capture import capture_learning_event

        await capture_learning_event(
            agent_name=self.name,
            event_type="error",
            context={
                "message": message[:500],
                "agent": self.name,
                "mcp_servers": self.mcp_servers,
            },
            outcome={
                "error_type": type(error).__name__,
                "error_message": str(error)[:500],
            },
        )

    # Maximum time (seconds) a single heartbeat work cycle can run
    HEARTBEAT_WORK_TIMEOUT = 300  # 5 minutes

    async def heartbeat(self) -> str:
        """
        Heartbeat check - called periodically by scheduler.

        Returns status: HEARTBEAT_OK or description of work done.
        """
        import time

        from agents.mission_control.learning.capture import capture_heartbeat

        self.logger.info("Heartbeat started")
        t0 = time.monotonic()

        # Record heartbeat in database
        await self._record_heartbeat()

        # Check for pending work
        work = await self._check_for_work()

        if work:
            self.logger.info("Found work to do", work_type=work.get("type"))
            try:
                result = await asyncio.wait_for(
                    self._do_work(work),
                    timeout=self.HEARTBEAT_WORK_TIMEOUT,
                )
                await capture_heartbeat(
                    agent_name=self.name,
                    found_work=True,
                    work_type=work.get("type"),
                    duration_seconds=time.monotonic() - t0,
                )
                return result
            except asyncio.TimeoutError:
                self.logger.warning(
                    "Heartbeat work timed out",
                    timeout=self.HEARTBEAT_WORK_TIMEOUT,
                    work_type=work.get("type"),
                )
                await capture_heartbeat(
                    agent_name=self.name,
                    found_work=True,
                    work_type=work.get("type"),
                    duration_seconds=time.monotonic() - t0,
                )
                return f"TIMEOUT after {self.HEARTBEAT_WORK_TIMEOUT}s"

        await capture_heartbeat(
            agent_name=self.name,
            found_work=False,
            work_type=None,
            duration_seconds=time.monotonic() - t0,
        )
        self.logger.info("No pending work")
        return "HEARTBEAT_OK"

    async def _record_heartbeat(self):
        """Persist last_heartbeat timestamp to the agents table."""
        from sqlalchemy import select

        from agents.mission_control.core.database import (
            Activity,
            ActivityType,
            AsyncSessionLocal,
        )
        from agents.mission_control.core.database import (
            Agent as AgentModel,
        )

        try:
            async with AsyncSessionLocal() as session:
                stmt = select(AgentModel).where(AgentModel.name == self.name)
                result = await session.execute(stmt)
                agent = result.scalar_one_or_none()
                if agent:
                    agent.last_heartbeat = datetime.now(timezone.utc)
                    agent.status = "active"
                    activity = Activity(
                        type=ActivityType.AGENT_HEARTBEAT,
                        agent_id=agent.id,
                        message=f"{self.name} heartbeat",
                    )
                    session.add(activity)
                    await session.commit()
                    self.logger.debug("Recorded heartbeat in DB")
        except Exception as e:
            self.logger.warning("Failed to record heartbeat", error=str(e))

    @abstractmethod
    async def _check_for_work(self) -> Optional[dict]:
        """Check for pending work. Override in subclass."""
        pass

    @abstractmethod
    async def _do_work(self, work: dict) -> str:
        """Do the work. Override in subclass."""
        pass
