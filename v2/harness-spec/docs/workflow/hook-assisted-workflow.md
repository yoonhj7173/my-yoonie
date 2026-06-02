# Hook-Assisted Workflow

The harness should not be fully hook-driven.

Use this architecture:

```txt
Agent contract = what each agent does
State machine = which agent runs next
Thin hooks = repeated side effects and safety checks
Markdown files = durable context and reports
Slack/Notion/mobile = external channels triggered by hooks
```

## Common flow

```txt
runAgent(agent)
→ agent performs agent-specific work
→ agent returns structured result
→ after_agent_run hook handles common side effects
→ state machine determines next agent
```

## Example

```txt
software_engineer finishes
→ after_agent_run:
   - write report
   - update latest.md
   - update progress.md
   - send Slack summary
   - persist run metadata
→ state machine:
   - if SUCCESS, run qa_engineer
   - if FAILED, route to recovery_engineer or retry
```

## Key rule

Do not put agent-specific reasoning into hooks.

Good hook responsibilities:

- Save report
- Update context
- Send Slack notification
- Log command result
- Enforce safety checks
- Request human approval
- Trigger escalation notification

Bad hook responsibilities:

- Decide product scope
- Write PRD
- Design architecture
- Write feature implementation strategy
- Generate QA scenarios
- Perform code review reasoning
