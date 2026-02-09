# Mission Control

> Autonomous multi-agent AI orchestration system powered by GitHub Copilot SDK

## Quick Start

```bash
pipx install mission-control   # or: pip install mission-control
mc setup                        # interactive wizard — configures everything
```

That's it. The wizard detects your system, authenticates GitHub, sets up the database, seeds agents, installs services, and starts them.

## What It Does

Mission Control runs a squad of AI agents that autonomously collaborate on software tasks. Agents pick up tasks, write code, open PRs, review each other's work, and report status — all coordinated through a shared database and MCP tools.

- **7 agents** by default — config-driven, zero custom code per agent
- **GitHub Copilot SDK** (GPT-4.1) as primary LLM
- **MCP tool integration** — GitHub, Telegram, DigitalOcean, custom tools
- **Deterministic health monitoring** — Vision Healer runs automated checks hourly
- **PR enforcement** — every task must produce a PR; transitions are fact-gated
- **SQLite or PostgreSQL** — zero-config SQLite default, PostgreSQL for production

## Architecture

```
Human (Telegram / Dashboard)
         │
    ┌────▼────┐    ┌──────────┐
    │ mc-bot  │    │ mc-api   │  ← FastAPI :8000 (dashboard, kanban, API)
    └────┬────┘    └────┬─────┘
         │              │
    ┌────▼──────────────▼──────┐
    │   Copilot SDK (GPT-4.1)  │  ← Session per agent, MCP passthrough
    └────┬──────────────┬──────┘
         │              │
    ┌────▼────┐   ┌─────▼──────┐
    │ mc-mcp  │   │mc-scheduler│  ← APScheduler, staggered heartbeats
    │ SSE:8001│   └────────────┘
    └────┬────┘
         │
    ┌────▼────┐
    │ SQLite/ │  ← Zero-config default
    │ Postgres│
    └─────────┘
```

**4 systemd services** run independently:

| Service | Port | Description |
|---------|------|-------------|
| `mc-api` | 8000 | FastAPI — dashboard, kanban board, chat, task management |
| `mc-mcp` | 8001 | Mission Control MCP server (SSE) — task/agent/document tools |
| `mc-bot` | — | Telegram bot — primary human interface |
| `mc-scheduler` | — | APScheduler — triggers agent heartbeats on staggered intervals |

## Agent Squad

All agents are config-driven via `workflows.yaml`. Only Jarvis (lead orchestration) and Vision (deterministic ops) have custom Python — every other agent is a `GenericAgent` configured purely through YAML.

### Lead Agents

| Agent | Role | Interval | Description |
|-------|------|----------|-------------|
| **Jarvis** | Squad Lead | 15 min | Decomposes work, delegates, reviews PRs, gatekeeps REVIEW→DONE |
| **Vision** | System Healer | 1 hour | Deterministic health monitor — 10 automated checks, auto-fixes |

### Specialists

| Agent | Role | Description |
|-------|------|-------------|
| **Friday** | Developer | Clean architecture, testing advocate |
| **Wong** | Documentation | Runbooks, technical docs, knowledge management |
| **Shuri** | Testing & QA | Edge cases, regression testing |
| **Fury** | Developer | Strategic, research-driven |
| **Pepper** | Developer | Pragmatic, ships fast |

### Scaling Agents

Edit `~/.mission-control/workflows.yaml` to add or remove agents. Default ships with **7 agents**.

**Memory estimate:** Each agent spawns a Copilot SDK session (~565 MB). The platform overhead (API, scheduler, MCP, DB) adds ~500 MB.

| Agents | RAM (est.) | Guidance |
|--------|------------|----------|
| 7 | ~4.5 GB | Default — full squad (Jarvis, Vision, Friday, Wong, Shuri, Fury, Pepper) |
| 5 | ~3.3 GB | Drop 2 specialists (e.g. remove Fury + Pepper) |
| 3 | ~2.2 GB | Core trio: Jarvis (lead), Friday (dev), Vision (ops) |
| 1 | ~1.1 GB | Jarvis only — single-agent mode |

> **Note:** If `workflows.yaml` is missing, the system falls back to a hardcoded 7-agent squad matching the defaults above. There is no silent agent inflation.

## Task Workflow

```
INBOX → ASSIGNED → IN_PROGRESS → REVIEW → DONE
                       │            │
                   (PR exists?) (Jarvis verifies)
                       │            │
                    No → ASSIGNED   No PR → ASSIGNED
```

**Mission core is pure workflow management.** Transitions are gated by factual checks (PR exists?), never by LLM response parsing. LLM output is stored as metadata for Vision to analyze asynchronously.

## CLI Commands

```bash
mc setup                          # Interactive setup wizard
mc status                         # Agent status + service health + memory
mc start                          # Start all systemd services
mc stop                           # Stop all services
mc logs [-f] [service]            # View/tail logs
mc config                         # Show configuration paths
mc serve                          # Start API server (foreground)
mc heartbeat [agent]              # Trigger heartbeat manually
mc run <agent> "message"          # Chat with an agent
mc task -t "title" -a friday      # Create and assign a task
mc standup                        # Generate daily standup
mc init-db                        # Initialize database schema
mc seed-agents                    # Seed agent records
mc telegram                       # Start Telegram bot (foreground)
```

## Configuration

All config lives in `~/.mission-control/` (or project root in dev mode):

| File | Purpose |
|------|---------|
| `.env` | GitHub PAT, database URL, Telegram token, MCP tokens |
| `workflows.yaml` | Agent definitions, missions, state transitions, guards |
| `mcp_servers.yaml` | MCP server definitions (command, args, env keys) |

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Yes | GitHub PAT with `repo` + `copilot` scopes |
| `DATABASE_URL` | — | Default: SQLite. Set `postgresql://...` for PG |
| `TELEGRAM_BOT_TOKEN` | Recommended | Without it, dashboard is your only visibility |
| `TELEGRAM_CHAT_ID` | With Telegram | Your Telegram chat ID |
| `DO_API_TOKEN` | — | DigitalOcean (for Quill infra monitor) |
| `TAVILY_API_KEY` | — | Web search for agents |

## HTTP API & Dashboard

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/dashboard` | Visual dashboard |
| GET | `/dashboard/kanban` | Kanban board (task swimlanes) |
| GET | `/agents` | List all agents |
| GET | `/tasks` | Tasks with pagination |
| POST | `/chat` | Chat with Jarvis |
| POST | `/chat/{agent}` | Chat with specific agent |
| POST | `/task` | Create a new task |
| GET | `/standup` | Daily standup summary |
| GET | `/workflow` | Current workflow config |
| GET | `/mcp/servers` | MCP server registry |
| POST | `/heartbeat/{agent}` | Trigger agent heartbeat |

## Vision Healer — Health Checks

10 deterministic checks run hourly (no LLM involved in detection):

| # | Check | Auto-Fix |
|---|-------|----------|
| 1 | Stale tasks (>1.5h no activity) | Reset to INBOX |
| 2 | Zombie processes (orphaned copilot/MCP) | Kill PIDs |
| 3 | Chatbot health | Alert human |
| 4 | Service health (4 systemd services) | Restart service |
| 5 | INBOX tasks with assignees | Transition → ASSIGNED |
| 6 | Log bloat (>50MB) | Truncate log files |
| 7 | Memory/swap pressure | Alert human |
| 8 | REVIEW tasks without PRs | Reset → ASSIGNED |
| 9 | Long-running tasks (>3h soft / >6h hard) | Alert or reset |
| 10 | Repo cleanliness (unauthorized changes) | Revert changes |

## Development

```bash
git clone https://github.com/Amaresh/agno-mission-control-copilot-sdk.git
cd agno-mission-control-copilot-sdk
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -q    # 72+ E2E tests
```

In dev mode, `paths.py` auto-detects the project root (via `pyproject.toml`) and uses it as `MC_HOME`. Set `MC_HOME=/custom/path` to override.

### Project Structure

```
mission-control/
├── src/mission_control/           # Main package (PyPI distributable)
│   ├── cli.py                     # Typer CLI (mc command)
│   ├── config.py                  # Pydantic settings
│   ├── paths.py                   # Centralized path resolver
│   ├── setup_wizard.py            # mc setup interactive wizard
│   ├── api.py                     # FastAPI HTTP API
│   ├── telegram_bot.py            # Telegram bot
│   ├── scheduler_main.py          # Heartbeat scheduler
│   ├── defaults/                  # Shipped configs (workflows, mcp, systemd)
│   ├── static/                    # Dashboard HTML
│   ├── mission_control/core/      # State machine, database, factory
│   ├── mission_control/mcp/       # MCP server + registry
│   └── squad/                     # Agent working dirs (SOUL.md, daily/)
├── tests/                         # 72+ E2E tests (no mocks)
├── infra/systemd/                 # Dev systemd service files
├── workflows.yaml                 # Active workflow config
├── mcp_servers.yaml               # Active MCP server definitions
└── pyproject.toml
```

## License

MIT
