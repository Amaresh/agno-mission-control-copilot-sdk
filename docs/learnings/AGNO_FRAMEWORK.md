# Agno Framework Learnings

This document captures the practical patterns used in Mission Control for building long-lived, tool-using agents with Agno.

## Why Agno worked well
- Python-native agent framework with tools and memory built in.
- MCP integration via `agno.tools.mcp.MCPTools`.
- Postgres-backed session storage via `agno.db.postgres.PostgresDb`.
- Plays well with local models (Ollama) and cloud fallbacks (Groq).

## Core agent lifecycle pattern
1. Instantiate a lightweight BaseAgent (metadata plus paths).
2. Lazy-create the Agno Agent in `_init_agent`.
3. Inject instructions (SOUL + WORKING + runtime guidance).
4. Use PostgresDb for persistent sessions.
5. Enable learning, memory, and history options for long-term context.

### Minimal skeleton (from BaseAgent)
```python
db = PostgresDb(db_url=..., session_table="agno_sessions")

agent = Agent(
    name=self.name,
    model=model,
    tools=all_tools,
    instructions=instructions,
    db=db,
    add_history_to_context=True,
    num_history_runs=5,
    learning=True,
    add_learnings_to_context=True,
    enable_agentic_memory=True,
    enable_user_memories=True,
    enable_session_summaries=True,
    markdown=True,
)
```

## Memory layout and persistence
Each agent owns a folder with explicit files:
```
agents/squad/{agent}/
  SOUL.md        # identity and personality
  WORKING.md     # current task state
  MEMORY.md      # curated long-term learnings
  daily/YYYY-MM-DD.md
```

Key learnings:
- Use file-based memory for deterministic state.
- Update WORKING.md on task start or completion.
- Append daily notes on each run for traceability.

## Model selection strategy (Agno side)
Mission Control uses a priority chain:
1. Copilot SDK (premium model, GPT-4.1)
2. Ollama (local model)
3. Groq (cloud fallback)

This keeps cost low while preserving quality for complex tasks.

## Tooling patterns
- Tools are defined with the `@tool` decorator and return strings.
- Parse flexible input formats (for example, JSON arrays or comma-separated lists).
- Keep tools asynchronous when they touch the database.

## Scheduler integration (heartbeat)
Agno does not require a built-in scheduler. Use APScheduler externally:
- Heartbeats run every 15 minutes with per-agent offsets.
- `_check_for_work()` decides if action is needed.
- `_do_work()` executes tasks and returns a status string.

## Lessons learned
- Keep Agno session persistence separate from async SQLAlchemy usage.
- Convert database URLs for PostgresDb (`postgresql+psycopg://`).
- Keep instruction building centralized to avoid divergent behavior.
- Align tool enums with database enums (status and priority values).

## Do and avoid
Do:
- Use lazy initialization for tools and models.
- Keep agent instructions stable and versioned.
- Keep memory updates explicit (WORKING and daily notes).

Avoid:
- Recreating agents per request.
- Storing state only in memory.
- Mixing async database sessions with PostgresDb connections.
