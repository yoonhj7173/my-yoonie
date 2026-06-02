# Claude Code Implementation Prompt

You are Claude Code working on my local AI harness project.

I want you to build a structured multi-agent workflow system for building MVPs end-to-end.

The goal is not to create an over-engineered autonomous agent swarm. The goal is to build a reliable, practical, CLI-based AI software-building workflow with clear agent roles, context management, reports, recovery loops, selective code reading, command execution, thin lifecycle hooks, and human approval gates.

## First, read these documents

Please read these specification files before coding:

- `docs/core-goals.md`
- `docs/folder-structure.md`
- `docs/workflow/state-machine.md`
- `docs/workflow/hook-assisted-workflow.md`
- `docs/hooks/thin-hooks.md`
- `docs/context/context-management.md`
- `docs/tools/tool-request-protocol.md`
- `docs/tools/codebase-map.md`
- `docs/tools/command-execution.md`
- `docs/tools/safety-and-approval.md`
- `docs/agents/product_manager.md`
- `docs/agents/system_architect.md`
- `docs/agents/software_engineer.md`
- `docs/agents/qa_engineer.md`
- `docs/agents/recovery_engineer.md`
- `docs/agents/code_reviewer.md`
- `docs/agents/devops_engineer.md`
- `docs/status-schema.md`
- `docs/implementation-priorities.md`



## Model provider and routing requirements

This harness must use OpenRouter as the main LLM provider from the first implementation phase.

Do not leave OpenRouter as a future placeholder only. Implement the provider abstraction and a working OpenRouter provider now.

The first implementation should still be small and practical, but the agent runner should be able to make a real OpenRouter LLM call when an API key is available.

### Provider abstraction

Create a simple provider abstraction such as:

- `harness/providers/base.py`
- `harness/providers/stub.py`
- `harness/providers/openrouter.py`

Implement:

- `BaseProvider`
- `StubProvider`
- `OpenRouterProvider`

The runner should call providers through one stable interface, for example:

```python
provider.generate(prompt_package, model_config)
```

The provider output must be compatible with report generation and the structured status block.

### OpenRouterProvider requirements

Implement real OpenRouter API calls in the first foundation phase.

Requirements:

- Read the API key from environment variable only, for example `OPENROUTER_API_KEY`.
- Never write API keys or secrets into files.
- If `OPENROUTER_API_KEY` is missing and the selected provider is OpenRouter, return a clear `NEEDS_USER_INPUT` or error message.
- Do not silently fall back to stub mode unless the user explicitly requests stub mode.
- Use a simple HTTP client implementation. Avoid unnecessary dependencies if the standard library is enough; if you choose a dependency, explain why.
- Support model name, temperature, and basic message payload.
- Save no secrets in logs.
- Log provider name, model tier, resolved model name, latency if available, and success/failure status.
- Do not implement streaming yet unless it is trivial and does not complicate the design.
- Do not implement complex fallback or cost-based routing yet.

Expected behavior:

- Default execution should use OpenRouter if configured.
- StubProvider should remain available for local testing, for example via a CLI flag such as `--provider stub` or config value.
- OpenRouterProvider should return a normalized result object that includes:
  - text/content
  - provider
  - model
  - status
  - raw usage/cost fields if available, but optional
  - error message if failed

### Prompt package

The runner should construct a prompt package from:

- agent YAML metadata
- agent Markdown instruction
- task prompt
- relevant context files
- run_id
- task_id

For Phase 0/1, keep prompt construction simple.

Do not implement full codebase map, tool protocol, or selective file reading yet unless the current phase explicitly asks for it.

### Agent specs: YAML + Markdown

Each agent should have both:

```txt
agents/<agent_name>.yaml
agents/<agent_name>.md
```

Use YAML for machine-readable metadata:

- name
- role_summary
- model config
- report filename
- default next agent
- context files
- output files
- allowed actions
- forbidden actions

Use Markdown for long-form human/LLM-readable instructions:

- role
- inputs
- context files to read
- responsibilities
- allowed actions
- forbidden actions
- output format
- done criteria
- failure conditions
- next agent

Do not put long-form agent instructions only in YAML.

### Model routing requirements

Implement simple model tier routing from the beginning.

Use model tiers instead of hardcoding final model names inside agent code.

Create a central model config file such as:

```yaml
models:
  strong: ""
  medium: ""
  cheap: ""
```

Agent YAML should reference model tiers:

```yaml
model:
  provider: openrouter
  tier: strong
  temperature: 0.2
```

The runner must resolve the tier into an actual model name before calling OpenRouter.

The first implementation should support:

- `strong`
- `medium`
- `cheap`

If `medium` is not needed immediately, it may still exist in config as a placeholder.

Initial routing intent:

```txt
product_manager: strong
system_architect: strong
software_engineer: strong
qa_engineer: cheap or medium
recovery_engineer: strong
code_reviewer: strong
devops_engineer: strong
```

The practical routing goal is:

- complex planning, architecture, coding, review, and recovery tasks use stronger Claude-class models
- simpler QA, reporting, summarization, and lightweight validation tasks may use cheaper DeepSeek-class models

Do not hardcode exact model IDs in source code.

Put model IDs in config only.

The config may include example placeholders, but the user should be able to update them without code changes.

Example:

```yaml
models:
  strong: "anthropic/<claude-model-id>"
  medium: ""
  cheap: "deepseek/<deepseek-model-id>"
```

For Phase 0/1, implement static tier-based routing only.

Do not implement yet:

- cost-based routing
- automatic model fallback
- model benchmarking
- dynamic router decisions
- multi-model voting
- recursive model escalation

### Stub mode status rule

Stub mode remains available for local testing.

In stub mode, no real LLM call is made. The runner returns a fake but structurally valid result.

If the stub provider completes successfully and the report/context files are written, use:

```txt
SUCCESS
```

Use:

```txt
SKIPPED
```

only when an agent was intentionally not run.

Do not use `SKIPPED` just because the LLM call is stubbed.

## Before making code changes, provide

1. Current codebase understanding
2. Proposed implementation plan
3. Files you plan to create/modify
4. Any assumptions
5. Any questions only if truly blocking

Then proceed with implementation.

## Implementation principles

- Prefer simple, maintainable architecture.
- Do not over-engineer.
- Do not introduce unnecessary dependencies.
- Do not rewrite the entire codebase unless absolutely necessary.
- Preserve existing functionality unless it conflicts with the new design.
- Implement by phase.
- Keep each step small and testable.

## Important architecture direction

Use:

- State machine for workflow routing.
- Agent specs for agent-specific responsibilities.
- Thin hooks for common side effects.
- Markdown files for persistent context and reports.
- Tool protocol for controlled file reads, writes, searches, and command execution.

Do not build a complex plugin ecosystem or autonomous multi-agent society.
