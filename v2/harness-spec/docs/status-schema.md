# Status Schema

Every agent run must have:

- `run_id`
- `task_id`
- `agent_name`
- `timestamp`
- `status`

Use these statuses consistently:

```txt
SUCCESS
FAILED
BLOCKED
NEEDS_USER_INPUT
SKIPPED
```

## Definitions

`SUCCESS`  
The agent completed its task successfully.

`FAILED`  
The agent attempted the task but the result failed.

`BLOCKED`  
The agent cannot proceed due to missing dependency, missing file, missing requirement, or environment issue.

`NEEDS_USER_INPUT`  
The agent needs a human decision.

`SKIPPED`  
The agent was intentionally not run.

## Structured Status Block

Every agent report must end with this machine-readable JSON block:

```json
{
  "run_id": "",
  "task_id": "",
  "agent": "",
  "status": "SUCCESS | FAILED | BLOCKED | NEEDS_USER_INPUT | SKIPPED",
  "summary": "",
  "files_requested": [],
  "files_created": [],
  "files_modified": [],
  "files_deleted": [],
  "commands_run": [],
  "command_results": [],
  "issues_found": [],
  "human_input_required": false,
  "next_recommended_action": "",
  "next_agent": ""
}
```
