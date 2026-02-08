# Reusable Code Patterns

This file collects the most reusable patterns from the Mission Control codebase.

## BaseAgent pattern
Key responsibilities:
- Holds agent metadata and memory file paths.
- Lazily initializes the Agno Agent.
- Chooses model priority (Copilot SDK, Ollama, Groq).
- Wraps run and heartbeat flows.

Template:
```python
class BaseAgent(ABC):
    def __init__(self, name, role, session_key, mcp_servers=None, heartbeat_offset=0):
        ...

    async def _init_agent(self) -> AgnoAgent:
        mcp_manager = MCPManager()
        mcp_tools = await mcp_manager.get_tools_for_agent(self.mcp_servers)
        for tool in mcp_tools:
            await tool.connect()
        all_tools = list(MISSION_CONTROL_TOOLS) + mcp_tools

        model = CopilotModel(...) or Ollama(...) or Groq(...)
        db = get_agno_db()
        return AgnoAgent(..., tools=all_tools, db=db)
```

## Tool definitions with @tool
Use the `@tool` decorator and keep inputs flexible:
```python
@tool(name="create_task", description="Create and assign a task.")
async def create_task(title: str, description: str = "", assignees: str = "", priority: str = "medium") -> str:
    assignee_list = parse_assignees(assignees)
    ...  # DB writes
    return "Created task ..."
```

## AgentFactory mapping
Keep a central registry for agent configuration:
```python
AGENT_CONFIGS = {"jarvis": {...}, "friday": {...}}

class AgentFactory:
    _instances = {}
    def get_agent(cls, name: str) -> BaseAgent:
        ...
```

## CopilotModel adapter
Key steps:
- Pass system_message to session config.
- Format history into a prompt string.
- Parse tool calls from JSON responses.
- Stream deltas and stop on SESSION_IDLE.

## API entry points
FastAPI endpoints wrap common actions:
- `/chat` and `/chat/{agent}` for agent conversation.
- `/task` for task creation.
- `/standup` for daily summary.
- `/heartbeat/{agent}` to trigger work.

## Learning capture pattern
Capture errors and tool usage for the learning system:
```python
await capture_learning_event(
    agent_id=session_key,
    event_type="error",
    context={"message": msg, "agent": name},
    outcome={"error_type": type(err).__name__, "error_message": str(err)},
)
```
