# Agno + Copilot SDK Learnings (Mission Control)

This folder consolidates the practical learnings from building Mission Control: an autonomous multi-agent system using the Agno framework, GitHub Copilot SDK, and MCP. It is intended as a reusable template for future projects.

## Scope
- Summarizes patterns implemented in this repo (BaseAgent, CopilotModel, MCPManager, tools, learning capture).
- Captures integration decisions for Agno, Copilot SDK, Ollama, and Groq.
- Documents architecture, code patterns, and troubleshooting notes.

## Contents
| Document | Description |
| --- | --- |
| AGNO_FRAMEWORK.md | Core Agno agent patterns, memory, and session configuration |
| COPILOT_SDK_INTEGRATION.md | CopilotModel design, session handling, and tool calling |
| MCP_PATTERNS.md | MCP server configuration for Agno and Copilot SDK |
| ARCHITECTURE.md | Mission Control architecture and data flow |
| CODE_PATTERNS.md | Reusable code snippets and templates |
| TROUBLESHOOTING.md | Common issues and fixes |

## Quick takeaways
1. Build agents as long-lived, stateful components, not stateless chat handlers.
2. Prefer a dedicated CopilotModel adapter to integrate Copilot SDK into Agno.
3. Preserve context twice: Agno history plus Copilot session prompt injection.
4. Treat MCP servers as first-class dependencies with explicit env configuration.
5. Keep database enums and tool inputs aligned across API, MCP server, and tools.

## How to reuse this template
1. Copy `agents/mission_control/core/base_agent.py` and `copilot_model.py`.
2. Keep the `MCPManager` plus `MISSION_CONTROL_TOOLS` pattern.
3. Wire settings in `agents/config.py` (model priority, tokens, DB URL).
4. Start with Copilot SDK when available, fallback to local Ollama, then Groq.
