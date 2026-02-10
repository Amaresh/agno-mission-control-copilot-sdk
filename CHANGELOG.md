# Changelog

All notable changes to `agno-mission-control` are documented in this file.

## [0.4.1] — 2026-02-10

### Added

- **Transition guard enforcement** — guards defined on workflow transitions are now
  evaluated at runtime before allowing state changes. If a guard fails (e.g. `has_research`
  check finds no file), the task stays in its current state and retries on the next heartbeat.
  Previously, guards were configured in `workflows.yaml` but never evaluated — tasks could
  reach DONE without any deliverables.
- **`default_config` merge** — mission-level `default_config` (e.g. `repository`) from
  `workflows.yaml` is now automatically merged into task config at mission init. Tasks no
  longer need `mission_config` pre-populated to inherit the correct repo and branch settings.

### Fixed

- **Learning pattern tests** tolerate empty DB state.

## [0.4.0] — 2026-02-10

### Added

- **Deep mission validation** — 12 checks at load time including state reachability,
  agent role coverage, guard registration, heartbeat stagger enforcement, dead state
  detection, and orphaned transition detection.
- **32 mission validation tests** covering all validation rules.
- **Content pipeline guards** — `has_research`, `has_draft`, `quality_approved`,
  `needs_revision`, `is_published`, `has_social_posts` — check file existence on GitHub.
- **Agent reassignment** via `state_agents` mapping on pipeline transitions.

### Fixed

- **All tests passing** — 192 pass, 0 fail, 4 skipped.
- **CRUD tests** use valid UPPERCASE TaskStatus states.
- **Atomic workflow reload** — preserves old state on failure.
- **CopilotModel tests** skip gracefully when copilot SDK not installed.

## [0.3.0] — 2026-02-09

### Added

- **Heartbeat integration tests** — 23 tests for staggered rotation, concurrent
  heartbeats, stuck task recovery, timeout escalation, guard evaluation.
- **Content marketing squad** — 8 agents for the research → draft → review → publish
  → promote pipeline.
- **SDK tool fix** — built-in SDK tools no longer overshadow MCP tools.
- **ETA calculation** — uses actual `heartbeat_interval` from workflows.yaml.
- **E2E test suites** — 54 tests for task lifecycle, heartbeats, mission CRUD.

## [0.2.0] — 2026-02-09

### Added

- **Visual Mission Builder** — drag-and-drop workflow designer.
- **Mission CRUD API** — `GET/POST/PUT/DELETE /api/missions`.
- **GenericMission engine** — config-only mission types via workflows.yaml.

## [0.1.0] — 2026-02-08

### Added

- Initial release with multi-agent orchestration, Agno framework integration,
  built-in build and content missions, CLI (`mc`), Kanban dashboard,
  Telegram integration, and MCP server support.
