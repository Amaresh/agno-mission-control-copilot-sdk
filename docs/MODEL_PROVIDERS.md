# Model Providers Guide

## Overview

Mission Control is designed to run on **moderate to low-spec hardware** by delegating all LLM inference to the **GitHub Copilot SDK**. No local GPU is required — the heavy lifting happens on GitHub's infrastructure.

This is a deliberate architectural choice: instead of needing expensive hardware to run local models, Mission Control uses the Copilot SDK to access premium models (GPT-4.1) for all 10 agents, keeping resource usage minimal on the host machine.

## Primary: GitHub Copilot SDK (Recommended)

The Copilot SDK is the default and recommended model provider.

### Prerequisites

- [GitHub Copilot CLI](https://docs.github.com/en/copilot) installed and authenticated (`copilot` in PATH)
- An active GitHub Copilot subscription

### Configuration

```bash
# .env
USE_COPILOT_SDK=true
COPILOT_MODEL=gpt-4.1
```

### How It Works

```
Agent heartbeat → CopilotModel → copilot CLI → GitHub API → GPT-4.1
                                                    ↓
                                              Response back
```

- Sessions are created per-request and destroyed after use (prevents process leaks)
- The SDK handles MCP tool calling natively — agents don't need separate tool execution logic
- Multi-turn context is maintained via Agno's `add_history_to_context` + SDK session persistence

### Why Copilot SDK?

| Aspect | Copilot SDK | Local Ollama |
|--------|-------------|--------------|
| **Hardware** | ~1GB RAM for Mission Control itself | 8-48GB+ VRAM for models |
| **Model quality** | GPT-4.1 (premium) | llama3.1:8b (good, not great) |
| **Speed** | Fast (cloud inference) | Slow on CPU, moderate on GPU |
| **Cost** | Included with Copilot subscription | Free but needs hardware |
| **Setup** | `copilot` CLI + auth | Install Ollama + pull models |

## Optional Fallbacks

If the Copilot SDK is unavailable, Mission Control can fall back to other providers. These are **optional** and not required for normal operation.

### Groq (Cloud Fallback)

```bash
# .env
GROQ_API_KEY=your_groq_api_key
```

Free tier with fast inference. Used as the first fallback if the Copilot SDK is unreachable.

### Ollama (Local Fallback)

```bash
# .env
OLLAMA_HOST=http://localhost:11434
```

Self-hosted local inference. Only recommended if you have adequate hardware (8GB+ VRAM) and need offline capability.

## Fallback Priority Chain

```
1. GitHub Copilot SDK (GPT-4.1)    ← primary, recommended
2. Groq (llama-3.3-70b-versatile)  ← cloud fallback (if GROQ_API_KEY set)
3. Ollama (llama3.1:8b)            ← local fallback (if Ollama running)
```

The fallback chain is implemented in `agents/mission_control/core/base_agent.py`. If the primary provider fails, it automatically tries the next available option.

## Configuration Reference

All model settings are in `agents/config.py` and can be overridden via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_COPILOT_SDK` | `true` | Use Copilot SDK as primary model |
| `COPILOT_MODEL` | `gpt-4.1` | Model ID for Copilot SDK |
| `GROQ_API_KEY` | — | Groq API key (enables Groq fallback) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint (enables Ollama fallback) |

## Per-Agent Model Override

All agents share the `COPILOT_MODEL` setting by default. If you want a specific agent
to use a cheaper/faster model (e.g. `gpt-5-mini` for Wong's docs work), you can
override it in five steps:

```bash
# 1. Open the agent's config in factory.py
nano agents/mission_control/core/factory.py

# 2. Find the agent block (e.g. "Wong") and add a model_id key:
#    "Wong": { ..., "model_id": "gpt-5-mini" }

# 3. Open base_agent.py and read self.model_id in _init_agent():
nano agents/mission_control/core/base_agent.py
#    Change:  model = CopilotModel(id=settings.copilot_model)
#    To:      model = CopilotModel(id=self.model_id or settings.copilot_model)

# 4. Restart the scheduler to pick up changes
sudo systemctl restart mc-scheduler

# 5. Verify in logs
journalctl -u mc-scheduler --since "1 min ago" | grep "Using Copilot"
```

Free-tier models to choose from:

| Model | Speed | Best For |
|-------|-------|----------|
| `gpt-4.1` | Standard | Complex code tasks (default) |
| `gpt-5-mini` | Fast | Lightweight tasks, docs, Q&A |
| `claude-haiku-4.5` | Fastest | Quick tool-calling, simple code |
| `claude-3.5-sonnet` | Standard | General-purpose alternative |
