# Thin Hooks

Use a thin hook layer, not a complex plugin framework.

## What hooks do

Hooks are lifecycle checkpoints. They let the harness run repeated side effects when something happens.

Example events:

```txt
after_agent_run
before_tool_execution
after_tool_execution
on_command_failure
on_human_approval_required
on_max_attempts_reached
```

## Recommended minimal interface

The implementation can be as simple as:

```ts
emit(eventName, payload)
on(eventName, handler)
```

Do not build a complex plugin ecosystem.

## P0 Hooks

### `after_agent_run`

When: after any agent finishes.

Purpose:

- Parse structured status block
- Write `/runs/<run-id>/<agent-report>.md`
- Update `/context/latest.md`
- Update `/context/progress.md`
- Persist run metadata
- Send Slack summary later
- Produce payload for state machine

### `before_tool_execution`

When: before READ/WRITE/EXEC/PATCH tools run.

Purpose:

- Block `/specs/prd.md` modification
- Block dangerous commands
- Require approval for production deploys
- Require approval for destructive DB operations
- Prevent writing secrets

### `after_tool_execution`

When: after any tool runs.

Purpose:

- Log tool result
- Save command output
- Track files read/modified
- Update codebase-map cache when files change

### `on_command_failure`

When: EXEC_COMMAND returns non-zero exit code.

Purpose:

- Save stdout/stderr
- Preserve stack trace
- Feed failure output back to agent loop
- Increment retry count

### `on_human_approval_required`

When: risky or ambiguous action needs human approval.

Purpose:

- Show approval request in terminal
- Later: send Slack approval request
- Pause workflow until approved/rejected

### `on_max_attempts_reached`

When: retry limit is reached.

Purpose:

- Stop the loop
- Create escalation report
- Notify user
- Pause workflow

## P1 Hooks

Implement later if needed:

- `before_agent_run`
- `before_codebase_map_generation`
- `after_codebase_map_generation`
- `before_file_read`
- `after_file_read`
- `before_file_write`
- `after_file_write`
- `before_context_load`
- `after_context_load`
- `on_status_change`
- `on_escalation`
- `on_workflow_complete`
- `on_workflow_paused`
- `on_workflow_resumed`
- `on_cost_threshold_reached`
- `on_model_fallback`
- `on_artifact_sync`

## Important rule

Hooks should handle common side effects.

Agent-specific outputs should remain in each agent spec.
