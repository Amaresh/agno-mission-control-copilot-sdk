# Custom Missions Guide

Mission Control uses a **config-only** mission system. You can define entirely new mission types — with custom pipelines, agents, actions, and prompts — by editing `workflows.yaml` and adding prompt `.md` files. **No Python code required.**

---

## How the GenericMission Engine Works

Every non-verify mission runs through the **GenericMission 5-step execute loop**:

```
┌──────────────────────────────────────────────┐
│  1. Determine Stage  →  task.status maps to  │
│     a stage config in workflows.yaml         │
│  2. Pre-Actions      →  gather context       │
│     (web search, read files, ensure branch)  │
│  3. Build Prompt     →  render .md template   │
│     with task variables + pre-action results  │
│  4. Run Agent (LLM)  →  generate content     │
│     (may call MCP tools for code tasks)      │
│  5. Post-Actions     →  commit deliverables  │
│     + transition to next stage + reassign    │
└──────────────────────────────────────────────┘
```

**State machine**: Tasks flow through stages defined by `states` and `transitions` in your mission config. When a stage's work is done (post-check passes or no post-check), the task advances to the next state and gets reassigned to the appropriate agent.

---

## workflows.yaml Schema Reference

### Agent Definitions

```yaml
agents:
  my-agent:
    name: MyAgent
    role: "Trend Researcher"        # Role name (used in state_agents mapping)
    model: claude-sonnet-4-20250514       # LLM model
    instructions: "You are a researcher..."
    mcp_servers: [github, tavily]   # MCP servers this agent can use
    always_run:                     # Optional: runs on every heartbeat
      prompt: "Do recurring work..."
      timeout: 180
```

### Mission Definition

```yaml
missions:
  my_mission:
    description: "What this mission does"
    initial_state: FIRST_STATE      # Starting state for new tasks
    verify_strategy: pr|file|none   # How Vision verifies completion
    
    default_config:                 # Default task config values
      repository: owner/repo

    states:                         # Custom TaskStatus values
      - FIRST_STATE
      - SECOND_STATE

    state_agents:                   # Which agent role handles each state
      FIRST_STATE: "Trend Researcher"
      SECOND_STATE: "SEO Writer"

    stages:                         # Per-state execution config
      FIRST_STATE:
        pre_actions:                # Actions before LLM runs
          - action: tavily_search
            query: "{title} topic keywords"
            max_results: 5
        prompt_base: my_base        # Optional: base template
        prompt_template: my_stage   # Stage-specific template
        post_check: pr_exists       # Optional: condition to advance
        post_actions:               # Actions after LLM runs
          - action: github_commit
            path: "output/{short_id}.md"
            message: "Add: {title}"

    transitions:                    # State machine transitions
      - from: FIRST_STATE
        to: SECOND_STATE
        guard: has_output           # Optional guard condition
      - from: SECOND_STATE
        to: DONE
```

### Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `states` | list[str] | Custom status values (auto-registered in DB at startup) |
| `initial_state` | str | First state for new tasks of this mission type |
| `verify_strategy` | `pr\|file\|none` | `pr`: Vision checks for open PRs. `file`: check deliverable exists. `none`: skip verification |
| `state_agents` | dict | Maps `STATE → "Agent Role"` for automatic hand-offs |
| `stages.<STATE>.pre_actions` | list | Actions to run before the LLM (context gathering) |
| `stages.<STATE>.prompt_base` | str | Base prompt template name (optional, for composite prompts) |
| `stages.<STATE>.prompt_template` | str | Stage-specific prompt template name |
| `stages.<STATE>.post_check` | str | `pr_exists` or `review_approved` — condition to advance |
| `stages.<STATE>.post_actions` | list | Actions to run after the LLM (commit deliverables) |
| `transitions` | list | State machine edges with optional `guard` conditions |

### Guard Enforcement

Guards are **evaluated at runtime** before each state transition. The execution flow is:

1. Agent runs the LLM with the stage prompt
2. `post_check` validates the LLM output (e.g. `[APPROVED]` in response)
3. `post_actions` persist deliverables (e.g. `github_commit`)
4. **Guard is evaluated** — if it fails, the transition is blocked and the task stays in its current state
5. On the next heartbeat, the agent retries from step 1

This guarantees that no task can advance to the next state without its deliverables actually existing. For example, `has_research` checks the GitHub API for a file in `content/research/` matching the task's short ID.

Register custom guards in `guards.py`:

```python
from mission_control.mission_control.core.guards import GuardRegistry

@GuardRegistry.register("my_custom_guard")
async def _my_guard(context: dict, session=None) -> bool:
    # context has: task_id, short_id, title, repository, owner, repo, etc.
    return some_check(context)
```

---

## Built-in Actions

Actions are used in `pre_actions` and `post_actions`. All parameters support `{variable}` substitution with task context.

### `tavily_search`
Web search via Tavily API. Requires `TAVILY_API_KEY` env var.

```yaml
pre_actions:
  - action: tavily_search
    query: "{title} relevant keywords"
    max_results: 5
```

### `github_read`
Read a file from a GitHub repository.

```yaml
pre_actions:
  - action: github_read
    path: "docs/{short_id}-brief.md"
    # repo defaults to task's repository config
```

### `github_commit`
Commit content to a GitHub repository.

```yaml
post_actions:
  - action: github_commit
    path: "output/{short_id}-result.md"
    message: "Add result for {title}"
    # content_source: uses LLM response by default
```

### `ensure_branch`
Create a git branch if it doesn't exist.

```yaml
pre_actions:
  - action: ensure_branch
    base: "{source_branch}"
    branch: "{branch_name}"
    repository: "{owner}/{repo}"
```

### Custom Actions

Register new actions in Python with the `@register_action` decorator:

```python
from agents.mission_control.core.actions import register_action

@register_action("my_action")
async def _my_action(params: dict, task_vars: dict) -> str:
    # params: from YAML config (with {variables} already rendered)
    # task_vars: {task_id, title, short_id, owner, repo, ...}
    result = await do_something(params["my_param"])
    return result  # returned as context for the prompt
```

---

## Prompt Template Authoring

Prompt templates are `.md` files in `agents/prompts/`. They use `{variable}` substitution.

### Available Variables

| Variable | Source |
|----------|--------|
| `{task_id}` | Full task UUID |
| `{short_id}` | First 8 chars of task ID |
| `{title}` | Task title |
| `{description}` | Task description |
| `{owner}` | Repository owner (from mission config) |
| `{repo}` | Repository name (from mission config) |
| `{source_branch}` | Base branch (from mission config) |
| `{branch_name}` | Agent working branch |
| `{pre_action_context}` | Combined output from all pre_actions |

### Simple Template

```markdown
# Research Brief: {title}

You are researching: {title}

## Context
{pre_action_context}

## Instructions
Write a comprehensive research brief covering key findings, data points,
and recommendations. Output as structured markdown.
```

### Composite Templates

Use `prompt_base` + `prompt_template` for shared context across stages:

```yaml
stages:
  RESEARCH:
    prompt_base: content_base       # shared instructions
    prompt_template: content_research  # stage-specific instructions
```

The base template renders first, then the stage template appends to it.

---

## Built-in Mission Reference

### Dev Squad (`build` mission)

**Pipeline**: `ASSIGNED → IN_PROGRESS → REVIEW → DONE`

| State | Agent Role | What Happens |
|-------|-----------|--------------|
| ASSIGNED | Developer | `ensure_branch` pre-action, agent writes code via MCP tools |
| IN_PROGRESS | Developer | Agent continues work, `pr_exists` post-check gates advancement |
| REVIEW | Developer | Vision runs verification (verify_strategy: pr) |

### Content Marketing Squad (`content` mission)

**Pipeline**: `RESEARCH → DRAFT → REVIEW → PUBLISH → PROMOTE → DONE`

| State | Agent Role | Pre-Actions | Post-Actions |
|-------|-----------|-------------|-------------|
| RESEARCH | Trend Researcher | `tavily_search` | `github_commit` research brief |
| DRAFT | SEO Writer | `github_read` research | `github_commit` draft article |
| REVIEW | Quality Editor | `github_read` draft | `github_commit` approved draft |
| PUBLISH | Publisher | `github_read` draft | `github_commit` published article |
| PROMOTE | Social Amplifier | `github_read` published | `github_commit` social posts |

---

## Step-by-Step: Adding a New Mission

Let's create a **QA Testing** mission with this pipeline:
`PLAN → EXECUTE → REPORT → DONE`

### 1. Define Agents

Add to `workflows.yaml` under `agents:`:

```yaml
agents:
  qa-planner:
    name: QAPlanner
    role: "QA Planner"
    model: claude-sonnet-4-20250514
    instructions: "You plan test strategies for software projects."
    mcp_servers: [github]

  qa-executor:
    name: QAExecutor
    role: "QA Executor"
    model: claude-sonnet-4-20250514
    instructions: "You execute test plans and report results."
    mcp_servers: [github]
```

### 2. Define the Mission

Add under `missions:`:

```yaml
missions:
  qa:
    description: "QA pipeline: plan → execute → report"
    initial_state: PLAN
    verify_strategy: file

    default_config:
      repository: owner/my-project

    states:
      - PLAN
      - EXECUTE
      - REPORT

    state_agents:
      PLAN: "QA Planner"
      EXECUTE: "QA Executor"
      REPORT: "QA Executor"

    stages:
      PLAN:
        pre_actions:
          - action: github_read
            path: "README.md"
        prompt_template: qa_plan
        post_actions:
          - action: github_commit
            path: "qa/{short_id}-test-plan.md"
            message: "qa: test plan for {title}"
      EXECUTE:
        pre_actions:
          - action: github_read
            path: "qa/{short_id}-test-plan.md"
        prompt_template: qa_execute
        post_actions:
          - action: github_commit
            path: "qa/{short_id}-results.md"
            message: "qa: test results for {title}"
      REPORT:
        pre_actions:
          - action: github_read
            path: "qa/{short_id}-results.md"
        prompt_template: qa_report
        post_actions:
          - action: github_commit
            path: "qa/{short_id}-report.md"
            message: "qa: final report for {title}"

    transitions:
      - from: PLAN
        to: EXECUTE
      - from: EXECUTE
        to: REPORT
      - from: REPORT
        to: DONE
```

### 3. Create Prompt Templates

Create `agents/prompts/qa_plan.md`:
```markdown
# Test Plan: {title}

{pre_action_context}

Create a comprehensive test plan for the above project. Include:
- Test scope and objectives
- Test cases (happy path + edge cases)
- Environment requirements
- Risk assessment
```

Create `agents/prompts/qa_execute.md` and `qa_report.md` similarly.

### 4. Create a Task

```bash
curl -X POST http://localhost:8000/task \
  -H "Content-Type: application/json" \
  -d '{
    "title": "QA: Authentication module",
    "mission_type": "qa",
    "assignees": ["qa-planner"],
    "repository": "owner/my-project"
  }'
```

### 5. Watch the Pipeline

The task will flow automatically: `PLAN → EXECUTE → REPORT → DONE`, with each stage handled by the appropriate agent, pre-actions gathering context, and post-actions committing deliverables.

---

## Tips & Best Practices

- **Keep stages focused** — each stage should have one clear deliverable
- **Use pre_actions for context** — gather everything the LLM needs before it runs
- **Use post_actions for persistence** — commit results to GitHub for traceability
- **Start with `verify_strategy: none`** for non-code missions (no PR to verify)
- **Test with forced heartbeats** — trigger `POST /heartbeat/{agent}` to test individual stages before letting the scheduler run automatically
- **Use composite prompts** for missions where agents share common context (e.g., product knowledge)
