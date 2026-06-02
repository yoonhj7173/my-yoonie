# Tool Request Protocol

Agents should request tools through structured JSON blocks.

Example:

```json
{
  "tool_requests": [
    {
      "tool": "READ_FILE",
      "paths": ["src/app.ts"],
      "reason": "Need to inspect app entry point."
    },
    {
      "tool": "SEARCH_CODE",
      "query": "createUser",
      "reason": "Find all usages before changing function signature."
    },
    {
      "tool": "EXEC_COMMAND",
      "command": "npm test",
      "reason": "Run tests after implementation."
    }
  ]
}
```

The harness should parse `tool_requests`, execute allowed tools, then provide structured results back to the agent.

Example result:

```json
{
  "tool_results": [
    {
      "tool": "EXEC_COMMAND",
      "command": "npm test",
      "exit_code": 1,
      "stdout": "...",
      "stderr": "...",
      "status": "FAILED"
    }
  ]
}
```

If a tool request requires approval:

```json
{
  "tool": "EXEC_COMMAND",
  "status": "NEEDS_USER_INPUT",
  "reason": "This command may modify production database.",
  "approval_required": true
}
```

## Minimum required tools

```txt
READ_FILE
SEARCH_CODE
EXEC_COMMAND
PATCH_FILE or WRITE_FILE
```

## Additional useful tools

```txt
READ_FILE_RANGE
LIST_FILES
```
