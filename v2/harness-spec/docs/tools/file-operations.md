# File Operation Tools

## READ_FILE

Read specific files requested by the agent.

Request:

```json
{
  "tool_requests": [
    {
      "tool": "READ_FILE",
      "paths": ["src/routes/user.ts", "src/models/user.ts"],
      "reason": "Need to inspect the user route and model because the error stack references them."
    }
  ]
}
```

Return:

- file path
- file content
- line numbers if possible
- file size warning if file is large

If a file is very large, suggest `READ_FILE_RANGE` or `SEARCH_CODE`.

## READ_FILE_RANGE

```json
{
  "tool_requests": [
    {
      "tool": "READ_FILE_RANGE",
      "path": "src/routes/user.ts",
      "start_line": 40,
      "end_line": 120,
      "reason": "Relevant handler appears to be in this range."
    }
  ]
}
```

## SEARCH_CODE

```json
{
  "tool_requests": [
    {
      "tool": "SEARCH_CODE",
      "query": "createUser",
      "reason": "Find all usages of createUser before modifying it."
    }
  ]
}
```

Return:

- matching file paths
- line numbers
- short snippets

Cap results and ask agent to narrow query if needed.

## PATCH_FILE / WRITE_FILE

Prefer PATCH_FILE when possible.

```json
{
  "tool_requests": [
    {
      "tool": "PATCH_FILE",
      "path": "src/routes/user.ts",
      "patch_summary": "Add null guard before accessing user.email",
      "reason": "Prevent runtime error when user is null."
    }
  ]
}
```

Use WRITE_FILE for new files or full replacements when justified.

All file changes must be recorded in the agent report.
