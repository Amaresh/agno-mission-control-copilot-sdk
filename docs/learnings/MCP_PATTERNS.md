# MCP Patterns

Mission Control uses MCP (Model Context Protocol) in two distinct ways: Agno MCPTools for standard agents and native MCP support in Copilot SDK sessions.

## Architecture: Hybrid SSE + Local Stdio

After extensive testing, we settled on a **hybrid approach**:

| MCP Server | Transport | Why |
|---|---|---|
| **mission-control** | SSE (persistent, port 8001) | Our own FastMCP server handles multiple concurrent clients. One process shared by all agent sessions. |
| **GitHub** | Local stdio (per-session) | Copilot CLI spawns/kills subprocess per session. Supergateway SSE bridge was tried but **only supports one concurrent SSE client** — crashes when multiple sessions connect. |

### Why not SSE for everything?
- `supergateway` (stdio→SSE bridge) crashes with "Already connected to a transport" when a second SSE client connects
- Only FastMCP (Python) properly supports multiple concurrent SSE clients
- For third-party stdio-only MCP servers (GitHub, Tavily), `type: "local"` is the only reliable option

### Why not local stdio for everything?
- `type: "local"` spawns a new subprocess per `create_session()` call
- Our mission-control MCP needs DB connections — spawning 40+ processes/hour is wasteful
- SSE lets all sessions share one persistent server with one DB connection pool

## Two integration paths
1. Agno MCPTools (for standard Agno agents)
2. Copilot SDK native MCP (for CopilotModel)

## Agno MCPTools pattern
Key pieces:
- `MCPManager` builds server configs from env-driven settings.
- `StdioServerParameters` ensures correct stdio initialization.
- `MCPTools` wraps the server as Agno tools.
- `tool_name_prefix` avoids naming collisions.

Example (SSE mode for persistent servers):
```python
mcp_tool = MCPTools(
    url="http://127.0.0.1:8001/sse",
    transport="sse",
    timeout_seconds=30,
    tool_name_prefix="mc_",
)
await mcp_tool.connect()
```

Example (stdio fallback):
```python
server_params = StdioServerParameters(
    command="npx",
    args=["-y", "@modelcontextprotocol/server-github"],
    env={"GITHUB_PERSONAL_ACCESS_TOKEN": settings.github_token},
)
mcp_tool = MCPTools(
    server_params=server_params,
    timeout_seconds=30,
    tool_name_prefix="github_",
)
await mcp_tool.connect()
```

**Warning**: Agno SSE connections are long-lived. If the server drops the connection,
you get `httpcore.RemoteProtocolError: peer closed connection`. The MCPTools instance
becomes permanently broken — there is no auto-reconnect. For Copilot SDK model, this
is less important since Copilot handles MCP natively.

## Copilot SDK native MCP
CopilotModel supports MCP servers directly via `mcp_servers` at session creation.

Current production config:
```python
mcp_servers = {
    "mission-control": {
        "type": "sse",
        "url": "http://127.0.0.1:8001/sse",
        "tools": ["*"],
    },
    "github": {
        "type": "local",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": settings.github_token},
        "tools": ["*"],
    },
}
```

### Critical: `tools: ["*"]` is REQUIRED
Without `"tools": ["*"]`, Copilot CLI **silently skips** the MCP server with:
```
No tools specified for server "X". Skipping server due to invalid configuration.
```
This error only appears in `~/.copilot/logs/process-*.log`, not in the bot log.

## Persistent MCP Server Lifecycle

The mission-control MCP server runs as a child process of the bot:
```python
# In telegram_bot.py _start_mcp_servers()
proc = await asyncio.create_subprocess_exec(
    venv_python, "-m", "agents.mission_control.mcp.mission_control_server",
    env={**os.environ, "MCP_PORT": "8001", "MCP_TRANSPORT": "sse"},
    stderr=open("logs/mcp-mission-control.log", "a"),  # NOT PIPE!
)
```

**Gotcha: stderr=PIPE causes crashes.** `asyncio.subprocess.PIPE` creates a buffer.
If nobody reads it, the buffer fills (64KB) and the subprocess blocks/dies. Always
redirect stderr to a file or DEVNULL for long-running MCP servers.

**Gotcha: Port conflicts on restart.** If the bot is killed (SIGKILL), the MCP child
process may survive as an orphan holding the port. The new bot's MCP server then fails
with `[Errno 98] address already in use`. Add startup port cleanup logic.

## Debugging MCP issues

1. **Copilot headless logs**: `~/.copilot/logs/process-{timestamp}-{pid}.log`
   - Shows MCP connection attempts, tool calls, errors
   - "Skipping server" = missing `tools: ["*"]`
   - "ECONNREFUSED" = MCP server not running on that port
2. **Bot log** (`/tmp/bot.log`): Shows heartbeat results, agent completion
3. **MCP server logs**: `logs/mcp-mission-control.log`, `logs/mcp-github.log`

## Pitfalls and fixes
- Missing env variables means the server will not be configured. Guard with `if settings.token`.
- npx requires Node 18+. Confirm Node is installed in deployment.
- Tool name collisions can happen. Use `tool_name_prefix` with Agno MCPTools.
- Long-running tool calls require higher timeouts (both Agno MCPTools and Copilot MCP).
- `supergateway` SSE bridge: single-client only. Do NOT use for multi-agent systems.
- Agno SSE MCPTools: no auto-reconnect. Connection death = permanent tool loss for that agent instance.

## Security
- Keep tokens in environment variables, not in source control.
- Avoid logging full token values.
- Use distinct tokens per environment (dev vs prod).
