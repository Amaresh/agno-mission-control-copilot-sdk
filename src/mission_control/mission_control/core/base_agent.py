"""
Base Agent class for Mission Control.

Uses Agno framework patterns:
- PostgresDb for session persistence
- Singleton agent instances (created once, reused)
- Session IDs for user/conversation tracking
- Learning for memory across sessions
"""

import os
import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any, Dict

import structlog
from agno.agent import Agent as AgnoAgent
from agno.models.ollama import Ollama
from agno.models.groq import Groq
from agno.db.postgres import PostgresDb

from mission_control.config import settings
from .copilot_model import CopilotModel, get_copilot_model

logger = structlog.get_logger()

# Shared database connection for all agents (Agno pattern)
_agno_db: Optional[PostgresDb] = None

def get_agno_db() -> Optional[PostgresDb]:
    """Get shared PostgresDb instance for Agno agents. Returns None on SQLite."""
    global _agno_db
    if _agno_db is None:
        if settings.database_url.startswith("sqlite"):
            logger.info("SQLite backend — Agno session storage disabled")
            return None
        # Convert async URL to psycopg format for Agno
        db_url = settings.database_url.replace(
            "postgresql://", "postgresql+psycopg://"
        )
        _agno_db = PostgresDb(
            db_url=db_url,
            session_table="agno_sessions",
        )
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
        always_run: Optional[dict] = None,
    ):
        self.name = name
        self.role = role
        self.session_key = session_key
        self.mcp_servers = mcp_servers or []
        self.heartbeat_offset = heartbeat_offset
        self.level = level
        self.always_run = always_run  # {"prompt": "...", "timeout": 60}

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
        self._repo_scope: Optional[str] = None

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
        from mission_control.mission_control.mcp.manager import MCPManager
        from mission_control.mission_control.tools import MISSION_CONTROL_TOOLS

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

        # Apply deferred repo scope (set_repo_scope() may have been called
        # before _init_agent, so propagate the persisted scope now).
        # Assign self._agent first so set_repo_scope can reach the model.
        self._agent = agent
        if self._repo_scope:
            self.set_repo_scope(self._repo_scope)

        return agent
    
    def _build_copilot_mcp_config(self) -> Dict[str, Any]:
        """Build MCP server config for Copilot SDK sessions.
        
        Uses MCPRegistry (mcp_servers.yaml) for server definitions.
        mission-control MCP is always injected as built-in.
        """
        from mission_control.mission_control.mcp.registry import get_mcp_registry
        registry = get_mcp_registry()

        mcp_servers = {}
        
        # Mission Control MCP — always injected (built-in)
        mcp_port = int(os.environ.get("MCP_PORT", "8001"))
        mc_tools = ["list_tasks", "list_agents", "get_my_tasks", "list_documents"]
        if self.level == "lead":
            mc_tools.extend(["update_task_status", "create_task", "assign_task"])
        mcp_servers["mission-control"] = {
            "type": "sse",
            "url": f"http://127.0.0.1:{mcp_port}/sse",
            "tools": mc_tools,
        }
        
        # Add each server from the agent's mcp_servers list via registry
        for server_name in self.mcp_servers:
            config = registry.get_server_config(server_name)
            if not config:
                continue

            wrapper_path = self._ensure_mcp_wrapper(
                name=server_name,
                command=config.command,
                args=config.args,
                env=config.env,
            )

            entry: Dict[str, Any] = {
                "type": "local",
                "command": wrapper_path,
                "args": [],
            }

            # Add tools allowlist if defined in mcp_servers.yaml
            tools = registry.get_tools_allowlist(server_name)
            if tools:
                entry["tools"] = tools

            mcp_servers[server_name] = entry
        
        return mcp_servers
    
    @staticmethod
    def _ensure_mcp_wrapper(
        name: str, command: str, args: list[str], env: dict[str, str],
    ) -> str:
        """Create/update a wrapper script that launches an MCP server with env vars."""
        import stat
        wrapper_path = f"/tmp/{name}-mcp-wrapper.sh"
        env_lines = "\n".join(f'export {k}="{v}"' for k, v in env.items())
        args_str = " ".join(f'"{a}"' for a in args) if args else ""
        script = f"""#!/bin/bash
{env_lines}
exec {command} {args_str} "$@"
"""
        try:
            with open(wrapper_path) as f:
                if f.read() == script:
                    return wrapper_path
        except FileNotFoundError:
            pass
        with open(wrapper_path, "w") as f:
            f.write(script)
        os.chmod(wrapper_path, stat.S_IRWXU)
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
        """Set (or clear) allowed-repo constraint on MCPTools and CopilotModel.

        The scope is persisted on self._repo_scope so that _init_agent() can
        apply it when the agent is lazily created (set_repo_scope is often
        called before run() triggers initialization).
        """
        self._repo_scope = repo

        from mission_control.mission_control.mcp.repo_scoped import RepoScopedMCPTools

        for t in self._mcp_tools:
            if isinstance(t, RepoScopedMCPTools):
                t.set_allowed_repo(repo)

        # Propagate to CopilotModel so SDK sessions also enforce the scope
        if self._agent and hasattr(self._agent.model, 'set_repo_scope'):
            self._agent.model.set_repo_scope(repo)

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
            from mission_control.mission_control.learning.capture import (
                get_relevant_patterns,
                format_patterns_for_context,
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
        """Capture error for federated learning."""
        from mission_control.mission_control.learning.capture import capture_learning_event

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
        from mission_control.mission_control.learning.capture import capture_heartbeat

        self.logger.info("Heartbeat started")
        t0 = time.monotonic()

        # Record heartbeat in database
        await self._record_heartbeat()

        # Phase 1: Run always_run prompt if configured (before task work)
        always_run_result = None
        if self.always_run and self.always_run.get("prompt"):
            ar_timeout = self.always_run.get("timeout", 60)
            try:
                self.logger.info("Running always_run prompt", timeout=ar_timeout)
                always_run_result = await asyncio.wait_for(
                    self.run(self.always_run["prompt"], user_id="system"),
                    timeout=ar_timeout,
                )
                self.logger.info(
                    "always_run complete",
                    result_len=len(always_run_result) if always_run_result else 0,
                )
            except asyncio.TimeoutError:
                always_run_result = "TIMEOUT: always_run exceeded time limit"
                self.logger.warning("always_run timed out", timeout=ar_timeout)
            except Exception as e:
                always_run_result = f"ERROR: always_run failed — {e}"
                self.logger.error("always_run failed", error=str(e))

            # Log to daily notes
            if always_run_result:
                summary = always_run_result[:300] if always_run_result else "No output"
                self.append_daily_note(f"## Always-Run\n{summary}")

        # Phase 2: Check for pending work
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

        found_work = always_run_result is not None
        await capture_heartbeat(
            agent_name=self.name,
            found_work=found_work,
            work_type="always_run" if found_work else None,
            duration_seconds=time.monotonic() - t0,
        )
        if not found_work:
            self.logger.info("No pending work")
        return always_run_result or "HEARTBEAT_OK"

    async def _record_heartbeat(self):
        """Persist last_heartbeat timestamp to the agents table."""
        from mission_control.mission_control.core.database import (
            AsyncSessionLocal,
            Agent as AgentModel,
            Activity,
            ActivityType,
        )
        from sqlalchemy import select

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

    async def _create_pr_fallback(
        self, repo_name: str, branch: str, base: str, title: str,
    ) -> tuple[bool, str]:
        """Create PR via GitHub API when LLM tool call fails (propagation delay)."""
        import asyncio
        import httpx
        from mission_control.config import settings as _s
        token = _s.github_token
        if not token or "/" not in repo_name:
            return False, ""
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            await asyncio.sleep(3)
            async with httpx.AsyncClient(timeout=15) as client:
                compare = await client.get(
                    f"https://api.github.com/repos/{repo_name}/compare/{base}...{branch}",
                    headers=headers,
                )
                if compare.status_code != 200 or compare.json().get("ahead_by", 0) == 0:
                    self.logger.warning("Branch has no commits ahead of base", branch=branch)
                    return False, ""
                resp = await client.post(
                    f"https://api.github.com/repos/{repo_name}/pulls",
                    headers=headers,
                    json={"title": title, "head": branch, "base": base},
                )
                if resp.status_code == 201:
                    pr_url = resp.json()["html_url"]
                    self.logger.info("Programmatic PR created", pr=pr_url)
                    return True, pr_url
                elif resp.status_code == 422:
                    self.logger.info("PR creation returned 422 — may already exist")
                    from mission_control.mission_control.core.pr_check import has_open_pr
                    return await has_open_pr(repo_name, f"{self.name.lower()}/")
                else:
                    self.logger.warning("PR creation failed", status=resp.status_code, body=resp.text[:200])
        except Exception as e:
            self.logger.warning("Programmatic PR creation failed", error=str(e))
        return False, ""

    async def _check_for_work(self) -> Optional[dict]:
        """Check for pending work. Override in subclass."""
        pass

    @abstractmethod
    async def _do_work(self, work: dict) -> str:
        """Do the work. Override in subclass."""
        pass
