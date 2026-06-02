# Core Goals

Build a workflow system with the following agents:

1. `product_manager`
2. `system_architect`
3. `software_engineer`
4. `qa_engineer`
5. `recovery_engineer`
6. `code_reviewer`
7. `devops_engineer`

## Main workflow

```txt
product_manager
→ user manually updates /specs/prd.md
→ user manually imports frontend/design files from Claude Design if needed
→ system_architect
→ software_engineer
→ qa_engineer
→ code_reviewer
→ devops_engineer
```

## Recovery loops

`recovery_engineer` is not part of the normal linear pipeline. It is triggered only as a recovery agent.

```txt
QA failure:
qa_engineer → recovery_engineer → software_engineer → qa_engineer

Code review failure:
code_reviewer → recovery_engineer or software_engineer → qa_engineer → code_reviewer

Deployment failure:
devops_engineer → recovery_engineer and/or devops_engineer → qa_engineer if needed → devops_engineer
```

## Design principles

Prioritize:

- Clear agent responsibilities
- Clear inputs and outputs
- Persistent markdown-based context
- Codebase map before full file reading
- Selective file reading
- Command execution with stdout/stderr/exit_code feedback
- Machine-readable status
- Human-readable reports
- Recovery loops
- Human approval gates
- Thin lifecycle hooks for common side effects
- Avoiding over-engineering
- Avoiding uncontrolled agent behavior

Do not add unnecessary complexity like autonomous agent societies, many parallel workers, recursive delegation, or multi-agent debate unless explicitly requested later.
