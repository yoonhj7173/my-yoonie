# Codebase Map Generator

The harness must not blindly send the full codebase to agents.

Before reading full file contents, provide relevant agents with a lightweight codebase map.

Relevant agents:

- `software_engineer`
- `recovery_engineer`
- `qa_engineer`
- `code_reviewer`
- `devops_engineer`

## Goal

```txt
Codebase Map
→ Selective File Read
→ Targeted Edit
→ Execute Command
→ Feed stdout/stderr/exit_code back
→ Retry with limit
→ Escalate if unresolved
```

## Map contents

Include:

- Directory tree
- Important config files
- Framework/language detected
- Package/build/test scripts
- Entry points
- Routes/controllers/services/models/components if detectable
- Exported functions/classes if detectable
- Test files
- Recently changed files
- Relevant reports/logs
- Known errors

## v1 acceptable implementation

Keep it lightweight:

- directory tree
- `package.json` / `pom.xml` / `build.gradle` / `next.config` / `tsconfig` summary if present
- exported function/class names using simple parsing/grep
- route/component/service/model file list based on filenames and folders
- test file list
- recently changed files if available from git

Avoid:

- Full file contents
- Huge output
- Heavy AST/LSP/call graph unless easy and already available
- Expensive analysis

## Save outputs

```txt
/runs/<run-id>/codebase-map.md
/cache/codebase-map.json
```

The cache can be regenerated when files change.
