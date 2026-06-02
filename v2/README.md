# my-yoonie

A terminal-native, multi-agent AI workflow harness. Builds products end-to-end — from free-form brainstorming through architecture, implementation, QA, code review, and deployment — using a deterministic state machine to orchestrate LLM agents.

---

## What it does

You describe what you want to build. The harness handles the rest.

```
product_manager → system_architect → software_engineer → qa_engineer → code_reviewer → devops_engineer
```

Each agent has a specific role and a strict tool budget. The state machine — not the LLM — decides what happens next. Agents cannot route themselves out of bounds.

---

## Key design principles

- **State machine is authoritative** — LLM `next_agent` suggestions are logged, never acted on
- **One feature at a time** — software_engineer implements and self-verifies features one by one before moving on
- **Bug loops** — when QA or code_reviewer finds issues, debugger_engineer automatically handles them and sends back for re-testing
- **Free-form → pipeline** — start with a conversation, transition to pipeline when ready
- **Cross-session memory** — key decisions and stack choices persist across runs
- **No LangChain, no agent frameworks** — pure Python, `urllib.request` for HTTP, `pyyaml` for config

---

## Getting started

**Requirements:** Python 3.11+, `pip install -e .`, `OPENROUTER_API_KEY` in env

Commands are available as `yoonie` (full) or `yn` (short). Both are identical.

```bash
cd ~/Desktop/Workspace/my-product
yoonie init          # scaffold dirs + template files
yoonie slack-setup   # optional: connect Slack channels
```

---

## Workflow

### 1. Chat mode — think before you build

Start a free-form conversation with the advisor agent. Discuss the product, explore ideas, make decisions. When ready, say "let's build" and the conversation context is handed off to the pipeline automatically.

```bash
yoonie chat                                          # open-ended
yoonie chat --task "want to build an SNS app"        # start with a topic
yoonie chat --no-pipeline                            # just talk, no build
```

The advisor detects intent (`let's start`, `pass to PM`, `start building`, etc.) and transitions automatically to whichever pipeline stage makes sense.

### 2. Pipeline — full build

```bash
# Full pipeline from product_manager
yoonie pipeline --start pm --task "Build a task management app for remote teams"

# Start from a specific stage (PRD and tech design already exist)
yoonie pipeline --start swe --task "Implement per specs/prd.md and specs/tech-design.md"

# List recent runs
yoonie pipeline --list

# Resume a paused pipeline
yoonie pipeline --resume <run-id>
```

**Agent aliases:** `pm`, `arch`, `swe`, `qa`, `cr`, `devops`, `dbg`

### 3. Session mode — interactive multi-turn

Run an agent in a loop, with conversation history maintained between turns.

```bash
yoonie session --start swe
# Inside: Enter = new line, Ctrl+D = submit, 'switch qa' = change agent, 'exit' = quit
```

### 4. Single agent run

```bash
yoonie run --agent pm --task "write PRD for auth module"
yoonie run -a arch -t "design the API layer"
```

---

## Pipeline mechanics

### Feature-by-feature loop

`software_engineer` implements exactly one feature per run. It reads `specs/implementation-plan.md`, picks the first unchecked item, implements it, self-verifies via `EXEC_COMMAND` (build, typecheck, lint, tests), checks it off, then signals `FEATURE_COMPLETE` to loop back for the next feature. When all features are checked off, it signals `SUCCESS` and QA takes over.

### Bug fix loop

When `qa_engineer` or `code_reviewer` returns `FAILED` (bugs found), the pipeline automatically routes to `debugger_engineer`. Debugger fixes the issues and sends back for re-testing. This loop continues until QA/reviewer passes or `max_attempts` is reached, at which point the pipeline escalates.

```
qa_engineer FAILED → debugger_engineer → qa_engineer (re-test)
code_reviewer FAILED → debugger_engineer → code_reviewer (re-review)
```

### Pause states

The pipeline pauses in two cases:

| State | Trigger | Action |
|-------|---------|--------|
| `waiting_for_human` | `requires_human_gate_after: true` in agent YAML | Review output, then `yoonie pipeline --resume <id>` |
| `needs_user_input` | Agent needs information it can't infer | Provide input via `--resume`, Slack DM, or conversation loop |

---

## Context & memory system

Three layers of context flow through the harness:

| Layer | File | Scope | Lifetime |
|-------|------|-------|----------|
| `HandoffNote` | `runs/{id}/handoff.json` | Agent pair | One handoff |
| `RunContext` | `runs/{id}/context.json` | Full pipeline run | Until run completes |
| `ProjectMemory` | `context/memory.json` | Project | Permanent |

**HandoffNote** — structured handoff between consecutive agents: decisions made, requirements, artifacts created, attempt history for bug-fix loops.

**RunContext** — shared state updated after every agent: stack choices, all files created, all decisions, test status. Automatically injected into every agent's prompt.

**ProjectMemory** — after each pipeline completes, a cheap LLM call extracts 3 key facts (stack, patterns, constraints) and appends them to `context/memory.json`. Every new pipeline starts with this context injected.

### mem0 backend (optional)

```yaml
# config/harness.yaml
memory:
  backend: mem0   # requires OPENAI_API_KEY + pip install mem0ai
```

---

## Agents

| Agent | Role | Max attempts |
|-------|------|-------------|
| `product_manager` | Product ideation, PRD draft | 1 |
| `system_architect` | Tech design, implementation plan | 2 |
| `software_engineer` | Feature implementation, self-verification | 5 |
| `qa_engineer` | Runtime testing, browser automation, bug finding | 3 |
| `code_reviewer` | Code quality, security, correctness | 2 |
| `devops_engineer` | Deployment | 3 |
| `debugger_engineer` | Deep debugging on FAILED/BLOCKED handoffs | 5 |
| `advisor` | Free-form conversation, pipeline entry point | 1 |

Each agent has:
- A YAML spec (`agents/<name>.yaml`) — model tier, tool budget, context files, max attempts
- A Markdown instruction file (`agents/<name>.md`) — role, workflow, allowed/forbidden actions

### Protected files

- `specs/prd.md` — human-only, no agent can modify it
- `specs/tech-design.md` — `system_architect` creates it once, no agent can overwrite it

---

## Tools available to agents

| Tool | Description |
|------|-------------|
| `READ_FILE` / `READ_FILE_RANGE` | Read files from codebase |
| `SEARCH_CODE` | Grep-style code search |
| `PATCH_FILE` | Apply targeted line-level edits |
| `WRITE_FILE` | Create new files |
| `EXEC_COMMAND` | Run shell commands (allowlisted, `shell=False`, timeout enforced) |
| `CODEBASE_MAP` | Cached directory tree |
| `BROWSER_NAVIGATE` / `BROWSER_CLICK` / `BROWSER_FILL` / `BROWSER_SCREENSHOT` / `BROWSER_EVAL` / `BROWSER_GET_TEXT` | Playwright browser automation for QA |

All mutations are audit-logged to `runs/{id}/mutations.jsonl` and `runs/{id}/commands.jsonl`.

---

## Slack integration

```bash
yoonie slack-setup        # interactive wizard
yoonie slack-listen       # start bot (Socket Mode)
```

**Outbound notifications** (webhook or bot token): agent completions, pipeline complete/paused/escalated, command failures.

**Slack bot commands** (DM):
```
pipeline: <task>            start full pipeline from PM
pipeline swe: <task>        start pipeline from a specific agent
run qa: <task>              run a single agent
status                      recent pipelines
help
```

When a pipeline is paused with `needs_user_input`, reply in the Slack thread to continue.

---

## Run artifacts

Every run writes to `runs/{run-id}/`:

```
pipeline.json               PipelineState checkpoint (crash-safe)
{agent}-assembled-prompt.md full prompt with context manifest
{agent}-meta.json           model / latency / token usage / cost
{agent}-report.md           agent output report
{agent}-spec.yaml           agent spec snapshot
handoff.json                latest HandoffNote
context.json                RunContext (shared state)
mutations.jsonl             file mutation audit log
commands.jsonl              EXEC_COMMAND audit log
logs/{slug}.log             full command output (when truncated)
escalation-report.md        written when max_attempts exceeded
```

Cost and token usage are also streamed to `logs/usage.jsonl` for dashboard ingestion.

---

## Configuration

```yaml
# config/harness.yaml
project: my-product
default_provider: openrouter   # openrouter | stub

memory:
  backend: json                # json | mem0

slack:
  enabled: false
  project_name: ""
  project_channel: ""
  control_channel: ai-harness-control
  webhook_url_env: SLACK_WEBHOOK_URL
  bot_token_env: SLACK_BOT_TOKEN
```

```yaml
# config/models.yaml
models:
  strong: "anthropic/claude-opus-4-5"
  medium: "anthropic/claude-sonnet-4-5"
  cheap:  "deepseek/deepseek-chat"
```

**Required env vars:**
- `OPENROUTER_API_KEY` — LLM calls (never written to files)
- `SLACK_BOT_TOKEN` / `SLACK_WEBHOOK_URL` — optional, Slack only
- `OPENAI_API_KEY` — optional, mem0 backend only

---

## CLI reference

Both `yoonie` and `yn` work identically. Use whichever you prefer.

```
yoonie init                                scaffold project dirs + template files
yoonie chat [-t TEXT] [--no-pipeline]      free-form conversation with advisor
yoonie run -a <agent> -t <task>            run a single agent
yoonie session --start <agent>             interactive multi-turn session
yoonie pipeline --start <agent> -t <task>  start a pipeline
yoonie pipeline --resume <run-id>          resume a paused pipeline
yoonie pipeline --list                     list recent pipelines
yoonie ralph -t <task> [-n N]              SE↔QA standalone loop
yoonie status                              show last run status
yoonie list                                list all agents
yoonie slack-setup                         configure Slack
yoonie slack-listen                        start Slack bot
```

Short form (`yn`):
```
yn chat
yn pipeline --start pm -t "build something"
yn pipeline --list
yn status
```

---

## Project structure

```
agents/             agent YAML + MD specs
config/             harness.yaml, models.yaml
context/            latest.md, progress.md, project.md, memory.json
specs/              prd.md, tech-design.md, implementation-plan.md, qa/review/deploy reports
runs/               per-run artifacts (one directory per run-id)
logs/               usage.jsonl
harness/            Python package
  pipeline.py       state machine orchestrator
  runner.py         agent execution + tool loop
  state.py          routing rules
  status.py         StatusCode, StatusBlock, HandoffNote, IssueItem
  context.py        RunContext
  memory.py         ProjectMemory (json + mem0 backends)
  conversation.py   interactive conversation loop
  ralph.py          SE↔QA standalone loop
  hooks.py          event system
  cli.py            CLI entry point
  tools/            file ops, exec, browser, codebase map, search
  integrations/     Slack notifier, Slack bot, input router
```

---

## License

MIT
