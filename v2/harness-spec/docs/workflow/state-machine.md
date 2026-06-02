# State Machine Workflow

Workflow routing should be controlled by a state machine or explicit orchestrator, not by free-form agent decisions.

## Manual-first stages

```txt
product_manager
→ user manually updates /specs/prd.md
→ user optionally imports Claude Design/frontend files
→ system_architect
```

The user is heavily involved in product and architecture phases.

## Automated stages

After `system_architect`, the workflow should be mostly automated:

```txt
software_engineer
→ qa_engineer
→ code_reviewer
→ devops_engineer
```

## Failure routing

```txt
qa_engineer FAILED
→ recovery_engineer
→ software_engineer
→ qa_engineer

code_reviewer FAILED
→ recovery_engineer or software_engineer
→ qa_engineer
→ code_reviewer

devops_engineer FAILED
→ recovery_engineer and/or devops_engineer
→ qa_engineer if needed
→ devops_engineer
```

## Why state machine, not hooks for routing?

Hooks are good for repeated side effects such as report writing, Slack notification, context update, and safety checks.

Workflow routing should stay explicit because it is core control flow.

Use:

```txt
State machine = next agent decision
Hooks = common side effects
Agent specs = agent-specific work
Markdown = durable memory/reports
```
