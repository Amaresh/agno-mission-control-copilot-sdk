# Copilot SDK Integration Learnings

Mission Control uses a custom Agno Model adapter (CopilotModel) to call GitHub Copilot SDK while still participating in Agno's tool and memory ecosystem.

## Key decisions
- Custom Model adapter: CopilotModel extends `agno.models.base.Model`.
- Session creation per request: create fresh Copilot sessions to avoid event handler state issues.
- System message passed via session config, not inside messages.
- MCP servers passed to `create_session()` for native tool calling.
- Dual context: Agno history plus prompt injection (belt and suspenders).
- Explicit timeouts (120s) for tool-heavy runs.
- User context set per request via `set_user_context()`.

## Architecture flow
1. Agno passes full message history to CopilotModel.ainvoke.
2. CopilotModel extracts the system message.
3. Messages are formatted into a prompt string (skipping system messages).
4. A Copilot session is created with `system_message` and `mcp_servers`.
5. SDK events stream deltas until SESSION_IDLE.

## Prompt formatting and history injection
- Include tool call summaries and tool results in the prompt.
- Add a list of available tools at the top of the prompt.
- Truncate long assistant messages to keep context bounded.

## Tool call parsing
- Copilot/GPT-4.1 can return JSON tool call payloads.
- If the response body is JSON containing a "name" field, parse as tool call.
- Wrap arguments as a JSON string for Agno tool invocation.

## Streaming support
- Listen for `SessionEventType.ASSISTANT_MESSAGE_DELTA` to build response.
- Use `SESSION_IDLE` as the completion signal.
- Use a timeout fallback to avoid hanging requests.

## Minimal usage
```python
model = CopilotModel(id="gpt-4.1")
model.set_mcp_servers(mcp_servers)

agent = Agent(
    name="Jarvis",
    model=model,
    tools=all_tools,
    instructions=instructions,
    db=db,
)
```

## Integration in BaseAgent
- BaseAgent chooses CopilotModel when `use_copilot_sdk` is true.
- BaseAgent builds MCP config:
  - mission-control server via local python module
  - GitHub MCP via npx server
- If Copilot SDK fails, fallback to Ollama, then Groq.

## Lessons learned
- Fresh sessions are more reliable than reusing a single session.
- Passing system_message in session config avoids duplication.
- Always include Agno history in the prompt; do not rely solely on SDK sessions.
- Tool calling requires MCP config to be passed at session creation.

## Future improvements
- Implement per-user session caching with safe invalidation.
- Add structured metrics for request timing and tool usage.
