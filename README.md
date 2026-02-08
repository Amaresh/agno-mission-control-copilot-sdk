# Mission Control

[![CI](https://github.com/Amaresh/agno-mission-control-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Amaresh/agno-mission-control-copilot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> **A squad of 10 AI agents that writes code, opens PRs, reviews work, and ships software — while you're away from your desk.** Send a task via Telegram from your phone, and your agents pick it up, break it down, implement it, and report back. The entire system runs on modest hardware (even a $12 cloud server) because all LLM inference is delegated to GitHub Copilot SDK — no local GPU, no expensive API bills. Under the hood, the Agno framework silently learns from every interaction: each heartbeat, each error fix, each completed task feeds back into agent prompts, so your squad gets measurably better at your codebase over days and weeks without any manual tuning.

> **Inspiration:** This project was inspired by [Bhanu Teja P's (@pbteja1998)](https://x.com/pbteja1998) original [Mission Control thread](https://x.com/pbteja1998/status/2017662163540971756) — a squad of 10 autonomous AI agents led by Jarvis that create work, claim tasks, communicate, review each other, and collaborate as a real team. That vision is the foundation this project builds on, adapted for GitHub Copilot SDK and the Agno framework.

## Overview

Mission Control is a self-orchestrating multi-agent system where 10 AI agents collaborate as a squad to deliver software. Agents work autonomously — picking up tasks, writing code, opening PRs, reviewing each other's work, and reporting status — coordinated through a shared PostgreSQL database, MCP tools, and a human interface via Telegram.

- **10 specialized agents** with persistent memory (SOUL.md, WORKING.md, daily logs)
- **GitHub Copilot SDK** (GPT-4.1) as primary LLM — runs on modest hardware, no local GPU needed
- **MCP tool integration** — GitHub, Telegram, DigitalOcean (swappable — see [Cloud Providers Guide](docs/CLOUD_PROVIDERS.md)), and a custom Mission Control MCP server
- **Deterministic health monitoring** — Vision Healer runs 10 automated checks hourly
- **Built-in Kanban dashboard** — real-time task board with agent status, ETA estimates, and activity feed
- **Learning analytics dashboard** — event timelines, per-agent performance, pattern discovery with confidence scores
- **Pull Request enforcement** — every task must produce a PR; no exceptions
- **Repo protection** — agents cannot modify mission-control source; GitHub MCP tools are restricted to read-only + issue/PR operations

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        Human (Telegram / HTTP API)                 │
└──────────┬──────────────────────────────────────┬──────────────────┘
           │                                      │
    ┌──────▼──────┐                        ┌──────▼──────┐
    │  mc-bot     │                        │  mc-api     │
    │  Telegram   │                        │  FastAPI    │
    │  :telegram  │                        │  :8000      │
    └──────┬──────┘                        └──────┬──────┘
           │                                      │
    ┌──────▼──────────────────────────────────────▼──────┐
    │              Copilot SDK (GPT-4.1)                  │
    │         Session management + MCP passthrough        │
    └──────┬──────────────────────────────────────┬──────┘
           │                                      │
    ┌──────▼──────┐                        ┌──────▼──────┐
    │  mc-mcp     │                        │  mc-scheduler│
    │  MCP Server │                        │  APScheduler │
    │  SSE :8001  │                        │  Heartbeats  │
    └──────┬──────┘                        └─────────────┘
           │
    ┌──────▼──────┐
    │  PostgreSQL  │
    │  + pgvector  │
    └─────────────┘
```

**4 systemd services** run independently:

| Service | Port | Description |
|---------|------|-------------|
| `mc-mcp` | 8001 | Mission Control MCP server (SSE). Exposes task/agent/document tools. |
| `mc-api` | 8000 | FastAPI HTTP API. Kanban board, chat, task management. |
| `mc-bot` | — | Telegram bot. Primary human interface for Jarvis. |
| `mc-scheduler` | — | APScheduler. Triggers agent heartbeats on staggered 15-min intervals. |

## Agent Squad

### Lead Agents

| Agent | Role | Schedule | Description |
|-------|------|----------|-------------|
| **Jarvis** | Squad Lead | 15 min | Receives tasks from human, decomposes work, delegates to specialists, reviews PRs, gatekeeps REVIEW→DONE transitions. |
| **Vision** | System Healer | 1 hour | Deterministic health monitor. Runs 10 automated checks, auto-fixes issues (zombie processes, stale tasks, memory pressure), reports via Telegram + GitHub Issues. No LLM for detection. |

### Developer Specialists

| Agent | Strengths | Schedule |
|-------|-----------|----------|
| **Friday** | Clean architecture, testing advocate, thorough understanding | 15 min |
| **Loki** | Methodical, edge-case obsessed, error handling | 15 min |
| **Pepper** | Pragmatic, ships fast, working code over perfect code | 15 min |
| **Wanda** | Full-stack (Python/APIs/frontend), clean interfaces | 15 min |
| **Fury** | Strategic, research-driven, deep understanding | 15 min |

### Specialist Roles

| Agent | Role | Schedule |
|-------|------|----------|
| **Quill** | Infrastructure Ops — monitors cloud resources via MCP ([swappable provider](docs/CLOUD_PROVIDERS.md)) | 15 min |
| **Wong** | Documentation — runbooks, technical docs, knowledge management | 15 min |
| **Shuri** | Testing & QA — edge cases, regression testing, UX focus | 15 min |

## Human in the Loop — Telegram

Mission Control is designed to run autonomously on a cheap server (a $12 DigitalOcean droplet, a spare laptop, a Raspberry Pi — anything with Python and a network connection). The human stays in the loop via **Telegram**, which means you can manage your entire squad from your phone.

### The Setup

```
┌──────────────┐          ┌─────────────────────────────────────┐
│  Your Phone  │◄────────►│  Your Server (home, cloud, anywhere)│
│  (Telegram)  │  internet│                                     │
│              │          │  Mission Control + 10 AI agents     │
│  "Add auth   │          │  running on systemd, heartbeating   │
│   to the     │          │  every 15 min, writing code, opening│
│   login API" │          │  PRs, reviewing each other's work   │
└──────────────┘          └─────────────────────────────────────┘
```

### Real-World Scenarios

**Scenario 1: Code from anywhere.** You're at the office, but your personal laptop at home is running Mission Control. You message Jarvis on Telegram: _"Add rate limiting to the /api/users endpoint in my-app repo."_ Jarvis decomposes it, assigns Friday to write the code and Shuri to write tests. 30 minutes later, two PRs show up on GitHub. You review and merge from your phone during lunch.

**Scenario 2: Your app goes down at a party.** Vision detects the health check failure and pings you on Telegram: _"⚠️ mc-api service is down, restarted automatically."_ If it's a code issue, you message Jarvis: _"The auth middleware is crashing on null tokens — fix it."_ Pepper picks it up, writes the fix, opens a PR. You merge from the Uber ride home.

**Scenario 3: Daily standup without a meeting.** Every evening, Jarvis sends you a summary: what each agent did, which PRs are open, what's blocked. You reply with new priorities. No Zoom call needed.

### Setting Up Your Telegram Bot

1. **Create a bot** — message [@BotFather](https://t.me/BotFather) on Telegram:
   ```
   /newbot
   Name: Mission Control
   Username: your_mc_bot
   ```
   Copy the bot token.

2. **Get your chat ID** — message [@userinfobot](https://t.me/userinfobot) and copy the ID.

3. **Configure** — add to your `.env`:
   ```bash
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   TELEGRAM_CHAT_ID=your_chat_id_here
   ```

4. **Start the bot**:
   ```bash
   systemctl --user start mc-bot
   # or: python -m agents.telegram_bot
   ```

5. **Talk to Jarvis** — open your bot in Telegram and send a message:
   ```
   /start          — intro + available commands
   /status         — system health check
   /agents         — list all agents
   /standup        — daily standup summary
   ```
   Or just send a natural language message — Jarvis handles the rest.

## Task Workflow

### Mission Architecture

Tasks are driven by **mission classes** — self-contained workflow engines that own the full lifecycle of a task phase. The factory dispatches to the correct mission based on `task.mission_type`.

```
                        ┌──────────────────┐
                        │   Task Created    │
                        │  mission_type     │
                        │  mission_config   │
                        └────────┬─────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                   │
     ┌────────▼────────┐  ┌─────▼──────┐   ┌───────▼───────┐
     │  BuildMission   │  │VerifyMission│   │ (TestMission) │
     │ ASSIGNED →      │  │ REVIEW →   │   │  future       │
     │ IN_PROGRESS →   │  │ DONE or    │   │               │
     │ REVIEW          │  │ ASSIGNED   │   │               │
     └─────────────────┘  └────────────┘   └───────────────┘
```

**BuildMission** (default): Branch creation → agent execution → error recovery → PR verification → REVIEW  
**VerifyMission**: PR lookup (task ID + agent prefix fallback) → approve to DONE or reject to ASSIGNED

### Task Config via `mission_config` (JSONB)

Every task carries a `mission_config` column with structured config instead of parsing freetext descriptions:

```json
{
  "repository": "owner/repo",
  "source_branch": "main",
  "context_files": ["src/api.py", "docs/spec.md"]
}
```

### Lifecycle Rules
- Every task must target a `repository` (via `mission_config` or description fallback)
- Every task must produce a Pull Request — no PR = not complete
- 1:1 assignment: each task → one primary agent
- Jarvis reviews: PR exists → DONE; no PR → back to ASSIGNED
- Vision auto-heals: stale tasks (>1.5h) reset to INBOX; long-running (>6h) flagged

## Kanban Dashboard

Mission Control ships with a **built-in Kanban board** — no separate frontend to install, no React build step. Open `http://localhost:8000/dashboard` and you have full visibility into your squad.

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│   INBOX     │  │  ASSIGNED   │  │ IN PROGRESS  │  │   REVIEW    │  │    DONE     │  │   BLOCKED   │
│             │  │             │  │              │  │             │  │             │  │             │
│ ┌─────────┐ │  │ ┌─────────┐ │  │ ┌──────────┐ │  │ ┌─────────┐ │  │ ┌─────────┐ │  │             │
│ │ Fix bug │ │  │ │ Add API │ │  │ │ Write    │ │  │ │ Review  │ │  │ │ Deploy  │ │  │             │
│ │ 🔴 high │ │  │ │ 🟡 med  │ │  │ │ tests   │ │  │ │ PR #42  │ │  │ │ config  │ │  │             │
│ │ friday  │ │  │ │ pepper  │ │  │ │ 🟢 low   │ │  │ │ jarvis  │ │  │ │ ✅ done │ │  │             │
│ │ ⚡ ~8m  │ │  │ │ ⏳ ~22m │ │  │ │ shuri   │ │  │ │         │ │  │ │         │ │  │             │
│ └─────────┘ │  │ └─────────┘ │  │ └──────────┘ │  │ └─────────┘ │  │ └─────────┘ │  │             │
└─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘
```

**What you see at a glance:**

| Panel | What It Shows |
|-------|--------------|
| **Stats bar** | Active agents, total tasks, completed count, heartbeats in last 24h |
| **Agent sidebar** | Each squad member with role, status badge (active/idle/error), last heartbeat time |
| **Task cards** | Title, description, priority badge (🔴🟡🟢), assignee tags, smart ETA |
| **Activity feed** | Last 20 non-heartbeat events — task transitions, status changes, messages |
| **Heartbeat log** | Last 30 heartbeats with agent names and timestamps |

**Smart ETA on every card** — The dashboard calculates when each task will actually start executing, based on the agent's 15-minute heartbeat cycle, their current queue position, and whether they're busy. You see ⚡ (soon, ≤15m), ⏳ (medium, 15–45m), or 🕐 (long, >45m) on every card.

Auto-refreshes every 30 seconds. Dark theme. Zero dependencies beyond the FastAPI server you're already running.

## Learning Analytics Dashboard

Open `http://localhost:8000/dashboard/learning` to see how your agents are actually performing — and improving — over time.

**Summary cards** at the top:

| Metric | What It Tracks |
|--------|---------------|
| **Total events** | Every heartbeat, task outcome, tool call, and error across all agents |
| **Heartbeats** | Count + average duration — spot agents that are slowing down |
| **Task outcomes** | Success/fail counts + success rate percentage |
| **Errors** | Total error count — rising trends signal problems |
| **Tool calls** | MCP tool usage frequency — see which tools agents rely on |
| **Patterns** | Learned patterns with confidence scores — the system's accumulated wisdom |

**Charts:**

- **Event Timeline** — line chart showing event counts by hour (heartbeats, task outcomes, errors, tool usage). Select time ranges: 6h, 24h, 3d, 7d.
- **Events by Agent** — doughnut chart showing which agents are doing the most work.

**Agent Performance Grid** — per-agent cards showing:
- Heartbeat count and average duration
- Task stats: total, successes, average completion time
- Error count
- Last heartbeat timestamp

**Learned Patterns Table** — every pattern the system has discovered, sorted by confidence:
- Pattern type (error fix, task strategy, tool usage)
- Trigger text (what activates the pattern)
- Confidence percentage
- Usage count and last used timestamp

> **Why this matters:** This isn't just metrics for metrics' sake. The patterns you see in this dashboard are the same patterns that get injected into agent prompts via `_enrich_with_learnings()`. When confidence rises on a pattern, agents are literally getting better at that specific scenario. You're watching your squad learn.

## Vision Healer — Health Checks

Vision runs **10 deterministic checks** every hour (no LLM involved in detection):

| # | Check | Auto-Fix |
|---|-------|----------|
| 1 | Stale tasks (>1.5h no activity) | Reset to INBOX |
| 2 | Zombie processes (orphaned copilot/MCP) | Kill PIDs |
| 3 | Chatbot health (service + last interaction) | Alert human |
| 4 | Service health (4 systemd services) | Restart service |
| 5 | INBOX tasks with assignees | Transition → ASSIGNED |
| 6 | Log bloat (>50MB) | Truncate log files |
| 7 | Memory/swap pressure (>90% RAM / >80% swap) | Alert human |
| 8 | REVIEW tasks without PRs | Reset → ASSIGNED |
| 9 | Long-running tasks (>3h soft / >6h hard) | Alert or reset |
| 10 | Repo cleanliness (unauthorized file changes) | Revert changes |

For code-level issues that can't be fixed by DB updates or service restarts, Vision shells out to `copilot` CLI (using the configured `COPILOT_MODEL`, default GPT-4.1). Alerts go to Telegram + a GitHub Issue (audit trail).

## Repo Protection

Agents are prevented from modifying the mission-control repository at two levels:

1. **GitHub MCP tool allowlist** — agents can only use read-only + issue/PR tools. File write tools (`create_or_update_file`, `push_files`, `delete_file`, `fork_repository`) are excluded.
2. **Vision Healer repo clean check** — hourly scan detects unauthorized file changes, reverts modified tracked files, removes untracked files. Agent working state (`daily/`, `WORKING.md`, `logs/`) is allowed.

## Quick Start

### Prerequisites

- **Linux** (Ubuntu 22.04+ recommended) — see [Platform Notes](#platform-notes) below
- Python 3.11+
- GitHub Copilot CLI (`copilot` in PATH) — primary LLM provider, no GPU required
- GitHub Personal Access Token — for MCP tools (issues, PRs, code search)
- Node.js 18+ (for GitHub MCP server via npx)
- PostgreSQL 16+ with pgvector
- A Telegram bot (create via [@BotFather](https://t.me/BotFather)) — your human-in-the-loop interface
- _(Optional)_ DigitalOcean API token — if using Quill agent for infrastructure monitoring

### Setup

```bash
cd agno-mission-control-copilot

python -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# Edit .env — at minimum: DATABASE_URL, GITHUB_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# Initialize database
python -m agents.cli init-db
python -m agents.cli seed-agents
```

### Running Locally (systemd)

```bash
# Install systemd user services
cp infra/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload

# Start all services
systemctl --user start mc-mcp mc-api mc-bot mc-scheduler

# Check status
systemctl --user status mc-mcp mc-api mc-bot mc-scheduler
```

### Running with Docker

```bash
cd infra/docker
docker-compose up -d postgres redis

# Start services natively (or via systemd above)
```

## CLI Commands

```bash
python -m agents.cli status              # Agent status overview
python -m agents.cli init-db             # Initialize database tables
python -m agents.cli seed-agents         # Seed agent records
python -m agents.cli run jarvis "msg"    # Chat with a specific agent
python -m agents.cli heartbeat friday    # Trigger agent heartbeat
python -m agents.cli task --title "..." --assign friday  # Create task
python -m agents.cli standup             # Generate daily standup
python -m agents.cli serve               # Start HTTP API server
python -m agents.cli start               # Start daemon with scheduler
```

## HTTP API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check |
| GET | `/agents` | List all agents |
| GET | `/tasks` | Kanban board (paginated DONE) |
| POST | `/chat` | Chat with Jarvis |
| POST | `/chat/{agent}` | Chat with specific agent |
| POST | `/task` | Create a new task (accepts `repository`, `source_branch` for mission config) |
| GET | `/standup` | Daily standup |
| POST | `/heartbeat/{agent}` | Trigger agent heartbeat |
| GET | `/dashboard` | **Kanban dashboard UI** |
| GET | `/dashboard/agents` | Agent status + heartbeat info (JSON) |
| GET | `/dashboard/tasks` | All tasks with ETA calculations (JSON) |
| GET | `/dashboard/activities` | Recent activity feed — last 24h (JSON) |
| GET | `/dashboard/learning` | **Learning analytics dashboard UI** |
| GET | `/dashboard/learning/stats` | Summary stats — events, success rates, patterns (JSON) |
| GET | `/dashboard/learning/timeline` | Event counts by hour — configurable range (JSON) |
| GET | `/dashboard/learning/agents` | Per-agent performance metrics (JSON) |
| GET | `/dashboard/learning/events` | Recent learning events with filters (JSON) |
| GET | `/dashboard/learning/patterns` | All learned patterns with confidence scores (JSON) |

## MCP Server Tools

The Mission Control MCP server (`mc-mcp`, port 8001) exposes these tools to agents:

| Tool | Access | Description |
|------|--------|-------------|
| `list_tasks` | All | Query tasks by status |
| `list_agents` | All | List squad members |
| `get_my_tasks` | All | Agent's assigned tasks |
| `list_documents` | All | Query deliverables |
| `create_task` | Lead | Create a new task (requires `repository`) |
| `assign_task` | Lead | Assign agent to task (auto-transitions INBOX→ASSIGNED) |
| `update_task_status` | Lead | Move task through workflow |

## Project Structure

```
agno-mission-control-copilot/
├── agents/
│   ├── cli.py                          # CLI interface
│   ├── config.py                       # Pydantic settings
│   ├── api.py                          # FastAPI HTTP API
│   ├── bot_main.py                     # Telegram bot entry point
│   ├── mcp_main.py                     # MCP server entry point
│   ├── scheduler_main.py              # Heartbeat scheduler entry point
│   ├── mission_control/
│   │   ├── core/
│   │   │   ├── base_agent.py           # BaseAgent (Agno + Copilot SDK)
│   │   │   ├── copilot_model.py        # CopilotModel (SDK wrapper)
│   │   │   ├── database.py             # SQLAlchemy models (Task, Agent, etc.)
│   │   │   ├── factory.py              # AgentFactory + GenericAgent (mission dispatch)
│   │   │   ├── pr_check.py             # PR existence checks for review gating
│   │   │   ├── missions/               # Mission workflow classes
│   │   │   │   ├── base.py             # BaseMission ABC
│   │   │   │   ├── build.py            # BuildMission: ASSIGNED→REVIEW
│   │   │   │   └── verify.py           # VerifyMission: REVIEW→DONE
│   │   │   └── migrations/
│   │   │       └── 001_add_mission_columns.sql
│   │   ├── mcp/
│   │   │   ├── mission_control_server.py  # FastMCP server (SSE :8001)
│   │   │   └── manager.py             # MCP tool management
│   │   ├── scheduler/
│   │   │   └── heartbeat.py            # APScheduler + agent registration
│   │   └── learning/
│   │       └── capture.py              # Learning event capture
│   └── squad/
│       ├── jarvis/                     # Squad Lead — task decomposition, PR review
│       │   ├── SOUL.md
│       │   └── agent.py
│       ├── friday/                     # Developer — clean code, architecture
│       ├── vision/                     # System Healer — health checks
│       │   ├── SOUL.md
│       │   ├── healer.py              # VisionHealer (deterministic, no LLM)
│       │   ├── checks.py             # 10 health check functions
│       │   └── notify.py             # Telegram + GitHub Issue alerts
│       ├── wong/                       # Documentation specialist
│       ├── shuri/                      # Testing & QA
│       ├── fury/                       # Research & analysis
│       ├── loki/                       # Developer — edge cases, error handling
│       ├── pepper/                     # Developer — pragmatic, ships fast
│       ├── quill/                      # Developer — rapid prototyping
│       └── wanda/                      # Developer — full-stack, APIs
├── docs/
├── infra/
│   ├── docker/
│   │   ├── docker-compose.yml          # PostgreSQL, Redis
│   │   └── init.sql
│   └── systemd/                        # mc-mcp, mc-api, mc-bot, mc-scheduler
├── tests/
├── logs/                               # Runtime logs (gitignored)
├── pyproject.toml
└── .env                                # Environment config (gitignored)
```

## Environment Variables

All configuration is via environment variables (`.env` file). See [`.env.example`](.env.example) for the full reference with descriptions.

### Required

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL 16+ connection string (with pgvector extension) |
| `GITHUB_TOKEN` | GitHub Personal Access Token — scopes: `repo`, `read:org` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (create via @BotFather) |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID (get via @userinfobot) |

### LLM (bring your own key)

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_COPILOT_SDK` | `true` | Use GitHub Copilot SDK — requires `copilot` CLI authenticated |
| `COPILOT_MODEL` | `gpt-4.1` | Model ID for Copilot SDK |
| `GROQ_API_KEY` | — | Optional cloud fallback ([console.groq.com](https://console.groq.com)) |
| `OLLAMA_HOST` | — | Optional local fallback (requires [Ollama](https://ollama.com) + GPU) |

### Cloud Infrastructure (optional — DigitalOcean default, [others supported](docs/CLOUD_PROVIDERS.md))

| Variable | Description |
|----------|-------------|
| `DO_API_TOKEN` | DigitalOcean API token — enables Quill agent to monitor Droplets, App Platform, and managed DBs |
| `DO_SPACES_KEY` | Spaces access key (for backups) |
| `DO_SPACES_SECRET` | Spaces secret key |
| `DO_SPACES_BUCKET` | Spaces bucket name (default: `mission-control-backups`) |

> **Using a different cloud?** See the [Cloud Providers Guide](docs/CLOUD_PROVIDERS.md) to swap Quill's MCP server from DigitalOcean to Railway, Hetzner, AWS Lightsail, or Vultr.

### Vision Healer (tunable)

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_MONITORED_SERVICES` | `mc-mcp,mc-api,mc-bot,mc-scheduler` | Comma-separated systemd services to monitor. Set empty to disable. |
| `VISION_ISSUE_REPO` | — | GitHub `owner/repo` for health alert issues |
| `VISION_RAM_THRESHOLD_PCT` | `90` | RAM usage % to trigger alert |
| `VISION_STALE_TASK_HOURS` | `1.5` | Hours before a task is flagged stale |

## LLM Strategy

Mission Control delegates all LLM inference to the **GitHub Copilot SDK**, so it runs on modest hardware — no local GPU required. All 10 agents share access to premium models (GPT-4.1) through the `copilot` CLI.

```
1. GitHub Copilot SDK (GPT-4.1)    ← primary (recommended)
2. Groq                             ← optional cloud fallback
3. Ollama (llama3.1:8b)             ← optional local fallback
```

Copilot SDK sessions are created per-request and destroyed after use to prevent process leaks. The SDK handles MCP tool calling natively — agents don't need separate tool execution logic.

See [docs/MODEL_PROVIDERS.md](docs/MODEL_PROVIDERS.md) for full provider configuration details.

## Agno Framework — Why It Matters

[Agno](https://github.com/agno-agi/agno) is not just a wrapper — it is the runtime that makes these agents genuinely improve over time. When you set `learning=True` on an Agno agent, it activates a **LearningMachine** that operates silently on every single LLM call:

1. **Before each call** — Agno's `build_context()` injects accumulated learnings (user profile, memories, session summaries) into the system prompt, so the model starts with everything it has learned so far.
2. **After each call** — `_aprocess_learnings()` extracts new insights from the conversation and persists them to the `agno_sessions` table. No manual bookkeeping needed.

Here's what you get out of the box just by having Agno in the stack:

| Capability | What It Does | Why It Helps |
|---|---|---|
| **Persistent memory** | `enable_user_memories=True` — remembers facts about the human across sessions | Agents don't re-ask questions you've already answered |
| **Session summaries** | `enable_session_summaries=True` — compresses long conversations | Context window stays efficient; old sessions aren't lost |
| **Conversation history** | `num_history_runs=5` — includes last 5 exchanges | Agents maintain coherence within a session |
| **Agentic memory** | `enable_agentic_memory=True` — lets agents decide what to remember | Important context survives across restarts and deployments |
| **Learning extraction** | `learning=True` + `add_learnings_to_context=True` | Model outputs measurably improve as interaction count grows |

On top of Agno's built-in learning, this project adds a **custom feedback loop** using two additional tables (`learning_events` and `learning_patterns`). Every heartbeat, task outcome, tool usage, and error fix is captured as a learning event. These are aggregated into patterns with confidence scores. Before each LLM call, `_enrich_with_learnings()` queries for relevant patterns by keyword match and injects them as additional context — so agents learn not just from conversations, but from operational history.

> **The practical result:** A fresh install starts with zero learnings. After a few days of normal operation, agents begin resolving known issues faster, avoiding repeated mistakes, and producing higher-quality PRs — without any model fine-tuning or manual prompt engineering.

## Agent Memory

Each agent maintains persistent memory across sessions:

| File | Purpose |
|------|---------|
| `SOUL.md` | Agent identity, personality, capabilities. Injected as system prompt. |
| `WORKING.md` | Current task context. Updated during heartbeat. |
| `daily/{DATE}.md` | Timestamped daily work log. Appended during heartbeat. |

## Platform Notes

Mission Control is built for **Linux**. The core agent loop (Copilot SDK, MCP tools, database) is platform-agnostic Python, but several components rely on Linux-specific tooling:

| Component | Linux Tool | What It Does | Required? |
|-----------|-----------|--------------|-----------|
| **Vision — zombie process detection** | `ps aux` | Finds orphaned `copilot --headless` and MCP server processes | Yes (Vision) |
| **Vision — process cleanup** | `os.kill(pid, SIGKILL)` | Terminates zombie processes | Yes (Vision) |
| **Vision — memory/swap monitoring** | `free -m` | Reads RAM and swap usage | Yes (Vision) |
| **Vision — service health** | `systemctl --user is-active` | Checks if systemd user services are running | Yes (Vision) |
| **Vision — service restart** | `systemctl --user restart` | Auto-restarts failed services | Yes (Vision) |
| **Vision — log bloat** | Python `os.stat` + file truncation | Detects and truncates logs >50MB | Cross-platform ✅ |
| **Vision — repo cleanliness** | `git status` / `git checkout` | Detects and reverts unauthorized file changes | Cross-platform ✅ |
| **Service management** | `systemd` user services | Runs mc-mcp, mc-api, mc-bot, mc-scheduler | Recommended |
| **MCP wrapper scripts** | `#!/bin/bash` | Injects API tokens into MCP server processes | Bash required |
| **Copilot SDK sessions** | `copilot` CLI | LLM inference via GitHub Copilot | Cross-platform ✅ |

**Running on macOS or Windows (WSL)?**

- **macOS**: The agent loop, dashboards, API, and Telegram bot will work. Vision's `systemctl` and `free -m` checks will fail gracefully (each check is wrapped in try/except). You can disable Vision or set `VISION_MONITORED_SERVICES=""` to skip service checks. For process monitoring, `ps aux` works on macOS. Memory checks would need adaptation (`vm_stat` instead of `free`).
- **Windows**: Use **WSL2** (Ubuntu). Native Windows is not supported — the bash wrapper scripts, `ps`, `systemctl`, and `free` commands won't work.
- **Docker**: The `docker-compose.yml` handles all OS dependencies inside containers. This is the easiest path for non-Linux hosts.

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Security Policy](SECURITY.md)

## License

[MIT](LICENSE)
