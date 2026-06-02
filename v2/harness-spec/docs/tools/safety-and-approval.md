# Safety and Human Approval

The harness must require human approval before:

- Finalizing PRD changes
- Modifying `/specs/prd.md`
- Major technical design changes
- Adding paid external services
- Changing database type
- Destructive DB operations
- Production deployment
- Modifying real secrets
- Deleting many files
- Changing authentication/security model
- Changing payment-related logic
- Running risky infrastructure commands
- Running production DB migrations

## Tool safety

Before executing tool requests, enforce safety rules.

Block or require approval for:

- destructive DB commands
- production deployment
- deleting many files
- modifying secrets
- changing auth/security model
- paid services
- risky infra commands
- production DB migrations
- commands that include `rm -rf` or similar destructive patterns
- commands that modify global system state

## Approval request format

Agents should request approval clearly with:

- What action is requested
- Why it is needed
- Risks
- Options
- Recommendation

## Hook tie-in

The minimal hook layer should use:

- `before_tool_execution` for safety checks
- `on_human_approval_required` for approval prompts
