# Mission Control Architecture

This document summarizes the architecture decisions and data flow for Mission Control.

## Overview
Mission Control is a multi-agent system with a shared brain (PostgreSQL + pgvector), MCP tool integrations, and optional Copilot SDK support for premium reasoning.

## Core components
- Agent squad: Jarvis, Friday, Vision, Wong, Shuri, Fury, Pepper.
- Shared brain: PostgreSQL stores tasks, messages, notifications, activities, documents, and learning data.
- Learning pipeline: learning_events feed learning_patterns.
- MCP integration: GitHub, Telegram, Tavily, Twilio, and a custom mission-control server.
- API layer: FastAPI for chat, tasks, heartbeats, and standups.
- Scheduler: APScheduler drives heartbeat checks.

## Data model summary
- agents: identity, role, status, heartbeat offset, MCP servers.
- tasks: title, status, priority, assignments.
- messages: task comments and conversations.
- notifications: mentions and assignments.
- activities: event log for auditing.
- learning_events and learning_patterns: agent learning backbone.

## Execution flow
1. Human or API sends a message to Jarvis.
2. Jarvis creates tasks and assignments in the shared brain.
3. Agents pick up work on heartbeat and update WORKING.md.
4. Activities and notifications are recorded for coordination.
5. Errors are captured as learning events for later aggregation.

## Model routing
- Primary: Copilot SDK (GPT-4.1) when enabled.
- Fallback: Ollama for local inference.
- Secondary fallback: Groq when local inference fails.

## Deployment notes
- Docker Compose runs Postgres, Redis, and Ollama locally.
- DigitalOcean droplet recommended for production-sized workloads.
- Use `.env` for model keys and MCP tokens.

## Design choices
- Agno chosen for MCP integration and memory features.
- Postgres chosen for durable shared state.
- File-based memory for deterministic agent state.

## Extension points
- Add agents by extending BaseAgent and AGENT_CONFIGS.
- Add MCP servers by expanding MCPManager.
- Add new tools in `agents/mission_control/tools.py`.
