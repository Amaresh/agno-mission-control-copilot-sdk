# Troubleshooting

Common issues and fixes observed while integrating Agno, Copilot SDK, and MCP.

## Copilot SDK issues
### Symptom: "No system message found"
Cause: system message not present in Agno message list.
Fix:
- Ensure BaseAgent builds instructions and Agno receives a system message.
- Do not inject system content into user messages.

### Symptom: tool calls not executed
Cause: MCP servers were not passed to Copilot session creation.
Fix:
- Call `CopilotModel.set_mcp_servers(...)` before usage.
- Ensure the MCP server configs are valid and not empty.

### Symptom: request hangs or times out
Cause: long tool calls or missing SESSION_IDLE event.
Fix:
- Keep a timeout in CopilotModel.
- Prefer fresh sessions per request.
- Reduce tool latency or increase timeout.

## MCP server issues
### Symptom: MCP tool not available
Cause: env var missing or server not configured.
Fix:
- Verify env vars exist (GITHUB_TOKEN, TELEGRAM_BOT_TOKEN, etc).
- Confirm Node 18+ is installed for npx servers.

### Symptom: Copilot silently skips MCP server ("nothing running")
Cause: Missing `"tools": ["*"]` in the MCP server config.
The error only appears in `~/.copilot/logs/process-*.log`:
```
No tools specified for server "X". Skipping server due to invalid configuration.
```
Fix: Add `"tools": ["*"]` to every MCP server config passed to Copilot SDK.

### Symptom: supergateway crashes with "Already connected to a transport"
Cause: supergateway SSE bridge only supports ONE concurrent SSE client.
When a second Copilot session connects, it crashes.
Fix: Don't use supergateway for multi-agent systems. Use `type: "local"` for
third-party stdio MCP servers (Copilot CLI manages the subprocess lifecycle).

### Symptom: MCP server fails with "[Errno 98] address already in use"
Cause: Orphaned MCP process from previous bot run holding the port.
The bot's finally block may not execute on SIGKILL.
Fix: Check and kill any process on the port before starting a new MCP server.

### Symptom: "Error in sse_reader" / "peer closed connection"
Cause: Agno MCPTools SSE connection is long-lived and has no auto-reconnect.
When the server drops the connection, the MCPTools instance is permanently broken.
Fix: For Copilot SDK model, this is benign — Copilot handles MCP natively.
For pure Agno agents, either recreate the MCPTools or use stdio transport.

### Symptom: MCP subprocess dies silently
Cause: `stderr=asyncio.subprocess.PIPE` creates a 64KB buffer. If nobody reads
from it, the buffer fills and the subprocess blocks/dies.
Fix: Redirect stderr to a file: `stderr=open("logs/mcp.log", "a")`

### Symptom: tool name collisions
Cause: multiple servers export the same tool names.
Fix:
- Use `tool_name_prefix` with Agno MCPTools.

## Database and enum mismatches
### Symptom: invalid status or priority errors
Cause: tool inputs do not match enum values.
Fix:
- Align tool inputs with TaskStatus and TaskPriority in the DB.
- Keep API, MCP server, and tools consistent.

### Symptom: AgentLevel enum mismatch crashing all heartbeats
Cause: PostgreSQL has lowercase enum values (`intern`, `specialist`, `lead`) but
SQLAlchemy `SQLEnum(PythonEnum)` defaults to using enum `.name` (UPPERCASE).
Every heartbeat crashes with `'specialist' is not among the defined enum values`.
Fix: Use `values_callable` on the SQLEnum:
```python
SQLEnum(AgentLevel, values_callable=lambda x: [e.value for e in x])
```
Note: AgentStatus works by coincidence — its names match the DB values (IDLE, ACTIVE, etc).

### Symptom: activity writes fail
Cause: ActivityType mismatch between code paths.
Fix:
- Ensure ActivityType names match the database enum constants.

## Ollama and Groq
### Symptom: Ollama connection refused
Fix:
- Verify Ollama is running and host is correct.
- Ensure the model is pulled (e.g., llama3.1:8b).

### Symptom: Groq fallback not used
Fix:
- Set GROQ_API_KEY in the environment.
- Ensure fallback_model is valid.

## Agent memory issues
### Symptom: WORKING.md not found
Fix:
- Ensure base path directories are created at agent init.
- Use default fallback content for empty memory files.

## General debugging tips
- Set log_level to DEBUG for structlog output.
- Confirm database connectivity before starting agents.
- Validate MCP servers manually with their CLI if needed.
