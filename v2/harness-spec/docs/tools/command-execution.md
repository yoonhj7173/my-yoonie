# Command Execution Rules

`EXEC_COMMAND` should:

- Run in the project root unless specified
- Capture stdout
- Capture stderr
- Capture exit_code
- Capture duration if possible
- Save logs under `/runs/<run-id>/logs/`
- Return structured result to the agent
- Enforce command safety checks before execution

Example request:

```json
{
  "tool_requests": [
    {
      "tool": "EXEC_COMMAND",
      "command": "npm test",
      "reason": "Validate whether the implementation passes tests."
    }
  ]
}
```

Example response:

```json
{
  "command": "npm test",
  "exit_code": 1,
  "stdout": "...",
  "stderr": "...",
  "status": "FAILED"
}
```

stdout, stderr, and exit_code are the source of truth.

If command output is too long:

- Truncate safely
- Include first relevant lines
- Include last relevant lines
- Preserve error stack traces
- Save full output to log file

When command execution fails, feed the error output back to the relevant agent loop.
