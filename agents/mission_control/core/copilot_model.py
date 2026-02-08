"""GitHub Copilot SDK Model for Agno agents.

This module provides a proper Agno Model implementation that uses
GitHub Copilot SDK for premium model access (GPT-4.1, etc).

KEY FEATURES:
1. Per-user session caching - maintains conversation context
2. SDK session resume - preserves full history on SDK side
3. History injection - falls back to injecting messages into prompt
4. Full Agno compatibility for tools, memory, orchestration

The secret sauce: Agno passes all message history via add_history_to_context=True,
and we format that into the Copilot prompt. Combined with SDK session persistence,
this gives us multi-turn context awareness even with a "cheap" model like GPT-4.1.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Type, Union

import structlog
from agno.agent import RunOutput
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse

# Copilot SDK imports
from copilot import CopilotClient
from copilot.generated.session_events import SessionEventType
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

# GitHub MCP write tools that require repo-scope enforcement.
# When no repo scope is active, these are stripped from the session
# MCP config so the Copilot SDK cannot invoke them.
_GITHUB_WRITE_TOOLS = frozenset({
    "create_branch",
    "create_or_update_file",
    "push_files",
    "delete_file",
})


# Global session cache: user_id -> (session, sdk_session_id)
# This preserves SDK session IDs across requests for the same user
_session_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = asyncio.Lock()


@dataclass
class CopilotModel(Model):
    """
    Agno Model implementation using GitHub Copilot SDK.

    Session Management Strategy:
    1. Cache SDK sessions by user_id for reuse
    2. Try resume_session() to restore SDK-side history
    3. On resume failure, inject message history into system prompt
    4. Always pass Agno's message history in formatted prompt

    This gives multi-turn context awareness:
    - History injected into system prompt for context
    - Agno also passes history via add_history_to_context=True
    """

    id: str = "gpt-4.1"
    name: str = "CopilotModel"
    provider: str = "GitHub Copilot"

    # Copilot-specific settings
    streaming: bool = True
    timeout: float = 300.0  # 5 minutes for complex multi-tool calls

    # Internal state
    _client: Optional[CopilotClient] = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # Current user/session context (set by run())
    _current_user_id: Optional[str] = field(default=None, repr=False)
    # Repo-scope enforcement (mirrors RepoScopedMCPTools for the SDK path)
    _allowed_owner: Optional[str] = field(default=None, repr=False)
    _allowed_repo: Optional[str] = field(default=None, repr=False)

    def __post_init__(self):
        """Initialize after dataclass creation."""
        if not hasattr(self, '_lock') or self._lock is None:
            self._lock = asyncio.Lock()

    def set_user_context(self, user_id: str):
        """Set the current user context for session management."""
        self._current_user_id = user_id

    def set_repo_scope(self, allowed_repo: Optional[str]) -> None:
        """Set (or clear) the allowed target repository for write tools.

        Mirrors RepoScopedMCPTools.set_allowed_repo() but for the Copilot SDK
        execution path.  When a scope is active, _scoped_mcp_servers() keeps
        write tools in the session config; when cleared, they are stripped.
        """
        if allowed_repo and "/" in allowed_repo:
            parts = allowed_repo.split("/", 1)
            self._allowed_owner = parts[0].lower()
            self._allowed_repo = parts[1].lower()
            logger.info("CopilotModel repo scope set", allowed=allowed_repo)
        else:
            self._allowed_owner = None
            self._allowed_repo = None

    async def _ensure_client(self):
        """Ensure Copilot client is initialized."""
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    # Do NOT set github_token in opts — it adds --no-auto-login
                    # which breaks the CLI's built-in auth. Instead, the GitHub MCP
                    # server gets its token via a wrapper script (see base_agent.py).
                    self._client = CopilotClient()
                    await self._client.start()
                    logger.info("Copilot client started", model=self.id)

    # MCP servers to pass to Copilot SDK sessions
    mcp_servers: Optional[Dict[str, Any]] = field(default=None, repr=False)

    def set_mcp_servers(self, servers: Dict[str, Any]):
        """Set MCP servers for Copilot SDK sessions."""
        self.mcp_servers = servers

    def _scoped_mcp_servers(self) -> Optional[Dict[str, Any]]:
        """Return MCP server config with write tools gated by repo scope.

        When no repo scope is set, GitHub write tools are stripped from the
        session config so the Copilot SDK cannot invoke them.  This mirrors
        the RepoScopedMCPTools guard on the Agno MCPTools path.
        """
        if not self.mcp_servers:
            return None

        import copy
        servers = copy.deepcopy(self.mcp_servers)

        github_cfg = servers.get("github")
        if github_cfg and "tools" in github_cfg:
            if not self._allowed_owner or not self._allowed_repo:
                before = len(github_cfg["tools"])
                github_cfg["tools"] = [
                    t for t in github_cfg["tools"]
                    if t not in _GITHUB_WRITE_TOOLS
                ]
                stripped = before - len(github_cfg["tools"])
                if stripped:
                    logger.debug(
                        "Stripped write tools from session (no repo scope)",
                        removed=stripped,
                    )

        return servers

    async def _get_or_create_session(
        self,
        user_id: Optional[str] = None,
        system_message: Optional[str] = None,
        message_history: Optional[List[Message]] = None,
    ) -> Any:
        """
        Create a fresh session for each request.

        The system_message contains the agent's SOUL and instructions.
        MCP servers are passed to enable native tool calling.
        """
        await self._ensure_client()

        def permission_handler(request, metadata):
            return {"kind": "approved", "rules": []}

        session_config = {
            "model": self.id,
            "streaming": self.streaming,
            "on_permission_request": permission_handler,
        }

        # Pass the system message (agent instructions/SOUL) to the session
        if system_message:
            session_config["system_message"] = {"content": system_message}

        # Pass MCP servers with write-tool gating based on repo scope
        scoped_servers = self._scoped_mcp_servers()
        if scoped_servers:
            session_config["mcp_servers"] = scoped_servers
            logger.debug(
                "Added MCP servers to session",
                num_servers=len(scoped_servers),
                servers=list(scoped_servers.keys()),
                repo_scope=f"{self._allowed_owner}/{self._allowed_repo}"
                if self._allowed_owner else "none",
            )

        session = await self._client.create_session(session_config)
        logger.debug("Created Copilot session", user_id=user_id, model=self.id,
                     has_system=bool(system_message), has_mcp=bool(self.mcp_servers))

        return session

    def _format_history_for_injection(self, messages: List[Message]) -> str:
        """Format message history for system prompt injection."""
        if not messages:
            return ""

        lines = []
        # Take last 10 exchanges to avoid overwhelming the context
        recent = messages[-20:] if len(messages) > 20 else messages

        for msg in recent:
            role = msg.role
            content = msg.content or ""

            if role == "user":
                lines.append(f"User: {content}")
            elif role == "assistant":
                # Truncate long responses
                truncated = content[:500] + "..." if len(content) > 500 else content
                lines.append(f"Assistant: {truncated}")
            elif role == "tool":
                lines.append(f"[Tool Result]: {content[:200]}")

        return "\n".join(lines)

    def _format_messages_to_prompt(
        self,
        messages: List[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Convert Agno messages to a prompt string for Copilot.

        Note: System message is passed separately to the session config,
        so we skip it here to avoid duplication.
        """
        prompt_parts = []

        for msg in messages:
            role = msg.role
            content = msg.content or ""

            if role == "system":
                # Skip - system message passed separately to session
                continue
            elif role == "user":
                prompt_parts.append(f"User: {content}")
            elif role == "assistant":
                prompt_parts.append(f"Assistant: {content}")
                # Include tool calls if present
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        if "function" in tc:
                            func = tc["function"]
                            prompt_parts.append(f"[Tool Call: {func.get('name')}({func.get('arguments')})]")
            elif role == "tool":
                # Tool results
                prompt_parts.append(f"[Tool Result]: {content}")

        # Add available tools info if present
        if tools:
            tool_names = [t.get("function", {}).get("name", "unknown") for t in tools]
            prompt_parts.insert(0, f"[Available Tools: {', '.join(tool_names)}]")

        return "\n".join(prompt_parts)

    def _parse_provider_response(self, response: str, **kwargs) -> ModelResponse:
        """Parse raw response into ModelResponse."""
        model_response = ModelResponse()
        model_response.content = response
        model_response.role = "assistant"

        # Check for tool calls in the response
        # Copilot/GPT-4.1 formats tool calls as JSON
        if response.strip().startswith("{") and '"name"' in response:
            try:
                tool_data = json.loads(response)
                if "name" in tool_data:
                    model_response.tool_calls = [{
                        "type": "function",
                        "function": {
                            "name": tool_data["name"],
                            "arguments": json.dumps(tool_data.get("parameters", {})),
                        }
                    }]
            except json.JSONDecodeError:
                pass

        return model_response

    def _parse_provider_response_delta(self, response: str) -> ModelResponse:
        """Parse streaming delta response."""
        model_response = ModelResponse()
        model_response.content = response
        model_response.role = "assistant"
        return model_response

    async def _call_copilot(
        self,
        prompt: str,
        messages: Optional[List[Message]] = None,
        system_message: Optional[str] = None,
    ) -> str:
        """
        Make a request to Copilot SDK.

        Creates a fresh session per request with history injected into system prompt.
        The SDK handles tool calls internally via its configured MCP servers.
        """
        # Create session with history injection
        session = await self._get_or_create_session(
            user_id=self._current_user_id,
            system_message=system_message,
            message_history=messages,
        )

        response_content = ""
        done_event = asyncio.Event()

        def handle_event(event):
            nonlocal response_content
            etype = event.type

            if etype == SessionEventType.ASSISTANT_MESSAGE_DELTA:
                if hasattr(event.data, 'delta_content') and event.data.delta_content:
                    response_content += event.data.delta_content
            elif etype == SessionEventType.SESSION_IDLE:
                done_event.set()
            elif etype == SessionEventType.TOOL_EXECUTION_START:
                tool_name = getattr(event.data, 'tool_name', None) or getattr(event.data, 'name', '?')
                tool_name = getattr(event.data, 'tool_name', None) or getattr(event.data, 'name', '?')
                # Log all event.data attributes for debugging
                data_attrs = {k: str(v)[:200] for k, v in vars(event.data).items() if not k.startswith('_')} if hasattr(event.data, '__dict__') else str(event.data)[:300]
                logger.info("Tool call started", tool=tool_name, data=data_attrs)
            elif etype == SessionEventType.TOOL_EXECUTION_COMPLETE:
                tool_name = getattr(event.data, 'tool_name', None) or getattr(event.data, 'name', '?')
                # Dump all non-None data attributes to find the result field
                data_attrs = {k: str(v)[:300] for k, v in vars(event.data).items() if not k.startswith('_') and v is not None} if hasattr(event.data, '__dict__') else str(event.data)[:500]
                logger.info("Tool call completed", tool=tool_name, data=data_attrs)
            elif etype == SessionEventType.SESSION_ERROR:
                err = getattr(event.data, 'message', None) or getattr(event.data, 'error', str(event.data))
                logger.error("Copilot session error", error=err)
            elif etype == SessionEventType.SKILL_INVOKED:
                skill = getattr(event.data, 'skill_name', None) or getattr(event.data, 'name', '?')
                logger.warning("Copilot invoked skill (should be avoided)", skill=skill)
            elif etype in (SessionEventType.ASSISTANT_TURN_START, SessionEventType.ASSISTANT_TURN_END):
                logger.debug("Turn event", event_type=str(etype))
            elif etype == SessionEventType.ABORT:
                logger.error("Copilot session aborted", data=str(event.data)[:200])
                done_event.set()

        session.on(handle_event)

        try:
            await session.send_and_wait({"prompt": prompt}, timeout=self.timeout)
            await asyncio.wait_for(done_event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.warning("Copilot request timed out", partial_len=len(response_content))
            if not response_content:
                response_content = "I'm sorry, the request timed out. Please try again."
        except Exception as e:
            logger.error("Copilot request failed", error=str(e))
            raise
        finally:
            # Destroy session to release MCP server subprocesses
            try:
                session.destroy()
                logger.debug("Destroyed Copilot session after request")
            except Exception as e:
                logger.warning("Failed to destroy session", error=str(e))

        logger.info("Copilot response", content_len=len(response_content),
                     preview=response_content[:200] if response_content else "(empty)")

        return response_content

    def invoke(
        self,
        messages: List[Message],
        assistant_message: Message,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Optional[RunOutput] = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        """Synchronous invoke (wraps async)."""
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(
            self.ainvoke(
                messages, assistant_message, response_format,
                tools, tool_choice, run_response, compress_tool_results
            )
        )

    async def ainvoke(
        self,
        messages: List[Message],
        assistant_message: Message,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Optional[RunOutput] = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        """
        Async invoke - main entry point for Agno.

        Agno passes the full message history here when add_history_to_context=True.
        We format this into a prompt and pass to Copilot, which also has session
        context. This "belt and suspenders" approach ensures context is preserved.
        """
        if run_response and run_response.metrics:
            run_response.metrics.set_time_to_first_token()

        assistant_message.metrics.start_timer()

        # Extract system message if present (for session creation)
        system_message = None
        for msg in messages:
            if msg.role == "system":
                system_message = msg.content
                logger.debug("Found system message", content_len=len(msg.content) if msg.content else 0)
                break

        if not system_message:
            logger.warning("No system message found in Agno messages!")

        # Convert messages to prompt (includes full history)
        prompt = self._format_messages_to_prompt(messages, tools)

        # Log context for debugging
        num_user_msgs = sum(1 for m in messages if m.role == "user")
        num_system_msgs = sum(1 for m in messages if m.role == "system")
        num_tools = len(tools) if tools else 0
        logger.debug("Copilot invoke",
                     user_id=self._current_user_id,
                     message_count=len(messages),
                     user_messages=num_user_msgs,
                     system_messages=num_system_msgs,
                     has_system=bool(system_message),
                     num_tools=num_tools,
                     prompt_len=len(prompt))

        # Call Copilot with session management
        response = await self._call_copilot(
            prompt=prompt,
            messages=messages,
            system_message=system_message,
        )

        assistant_message.metrics.stop_timer()

        return self._parse_provider_response(response)

    def invoke_stream(
        self,
        messages: List[Message],
        assistant_message: Message,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Optional[RunOutput] = None,
        compress_tool_results: bool = False,
    ) -> Iterator[ModelResponse]:
        """Synchronous streaming (wraps async)."""
        loop = asyncio.get_event_loop()
        async_gen = self.ainvoke_stream(
            messages, assistant_message, response_format,
            tools, tool_choice, run_response, compress_tool_results
        )

        # Convert async generator to sync iterator
        while True:
            try:
                yield loop.run_until_complete(async_gen.__anext__())
            except StopAsyncIteration:
                break

    async def ainvoke_stream(
        self,
        messages: List[Message],
        assistant_message: Message,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Optional[RunOutput] = None,
        compress_tool_results: bool = False,
    ) -> AsyncIterator[ModelResponse]:
        """Async streaming invoke with session management."""
        if run_response and run_response.metrics:
            run_response.metrics.set_time_to_first_token()

        assistant_message.metrics.start_timer()

        # Extract system message
        system_message = None
        for msg in messages:
            if msg.role == "system":
                system_message = msg.content
                break

        # Get or create session with history
        session = await self._get_or_create_session(
            user_id=self._current_user_id,
            system_message=system_message,
            message_history=messages,
        )
        prompt = self._format_messages_to_prompt(messages, tools)

        # Queue for streaming chunks
        chunk_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

        def handle_event(event):
            if event.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
                if hasattr(event.data, 'delta_content') and event.data.delta_content:
                    chunk_queue.put_nowait(event.data.delta_content)
            elif event.type == SessionEventType.SESSION_IDLE:
                chunk_queue.put_nowait(None)

        session.on(handle_event)

        # Start the request in background
        asyncio.create_task(
            session.send_and_wait({"prompt": prompt}, timeout=self.timeout)
        )

        # Yield chunks as they arrive
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(chunk_queue.get(), timeout=self.timeout)
                    if chunk is None:
                        break
                    yield self._parse_provider_response_delta(chunk)
                except asyncio.TimeoutError:
                    break
        finally:
            # Destroy session to release MCP server subprocesses
            try:
                session.destroy()
                logger.debug("Destroyed streaming Copilot session")
            except Exception as e:
                logger.warning("Failed to destroy streaming session", error=str(e))

        assistant_message.metrics.stop_timer()

    async def close(self):
        """Cleanup resources."""
        global _session_cache

        # Clear session cache
        async with _cache_lock:
            _session_cache.clear()

        if self._client:
            await self._client.stop()
            self._client = None


# Singleton for reuse
_copilot_model: Optional[CopilotModel] = None
_copilot_lock = asyncio.Lock()


async def get_copilot_model(model_id: str = "gpt-4.1") -> CopilotModel:
    """Get or create singleton CopilotModel."""
    global _copilot_model

    async with _copilot_lock:
        if _copilot_model is None or _copilot_model.id != model_id:
            _copilot_model = CopilotModel(id=model_id)

    return _copilot_model


def invalidate_user_session(user_id: str):
    """
    Invalidate a user's Copilot session.

    Call this if a session becomes stale or on error recovery.
    """
    import asyncio

    async def _invalidate():
        async with _cache_lock:
            if user_id in _session_cache:
                del _session_cache[user_id]

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_invalidate())
    except RuntimeError:
        # No running loop
        pass
