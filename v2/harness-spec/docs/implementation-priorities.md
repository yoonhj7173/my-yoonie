# Implementation Priorities

Implement in this order:

## Phase 1: Foundation

- Folder/context/report structure
- Run/task metadata and status schema
- Agent definitions/spec loading
- Basic state machine
- PRD read-only protection

## Phase 2: Thin Hooks

- Minimal event interface: `emit(eventName, payload)` and `on(eventName, handler)`
- `after_agent_run`
- `before_tool_execution`
- `after_tool_execution`
- `on_command_failure`
- `on_human_approval_required`
- `on_max_attempts_reached`

Keep hooks thin. Do not build a plugin ecosystem.

## Phase 3: Codebase Map

- Lightweight codebase map generator
- `/runs/<run-id>/codebase-map.md`
- `/cache/codebase-map.json`

## Phase 4: Tool Protocol

- Tool request JSON parser
- `READ_FILE`
- `READ_FILE_RANGE`
- `SEARCH_CODE`
- `PATCH_FILE` or `WRITE_FILE`

## Phase 5: Command Execution

- `EXEC_COMMAND`
- stdout/stderr/exit_code capture
- log files
- output truncation
- risky command blocking

## Phase 6: Engineer and Recovery Loops

- `software_engineer` loop
- `recovery_engineer` loop
- max attempts = 3
- escalation report

## Phase 7: QA/Review/DevOps Reports

- QA report
- review report
- deploy report
- health check basics

## Phase 8: External Integrations Later

- Slack notifications
- Slack approval
- Notion sync
- mobile control
- dashboard
- cost tracking

Do not implement Phase 8 until the core workflow is stable.
