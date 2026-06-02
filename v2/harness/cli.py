from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _find_project_root() -> Path:
    """Walk up from cwd looking for config/harness.yaml as the project root marker."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "config" / "harness.yaml").exists():
            return parent
    return cwd


def _resolve_provider(args: argparse.Namespace, project_root: Path) -> str:
    if getattr(args, "provider", None):
        return args.provider
    # Fall back to config
    try:
        import yaml  # noqa: PLC0415
        cfg_path = project_root / "config" / "harness.yaml"
        if cfg_path.exists():
            with cfg_path.open() as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("default_provider", "openrouter")
    except Exception:
        pass
    return "openrouter"


# ---------------------------------------------------------------------------
# Interactive input helpers
# ---------------------------------------------------------------------------

def _read_task_interactive(prompt_label: str = "") -> str | None:
    """
    Print a prompt and read multiline input until Ctrl+D (EOF).
    Returns None if the user types 'exit' or 'quit' on a line by itself,
    or submits empty input.
    """
    if prompt_label:
        print(prompt_label)
    print("Task (Ctrl+D to submit, 'exit' to quit):")
    lines: list[str] = []
    try:
        while True:
            line = input()
            if line.strip().lower() in ("exit", "quit") and not lines:
                return None
            lines.append(line)
    except EOFError:
        pass

    text = "\n".join(lines).strip()
    return text if text else None


def _get_project_name(project_root: Path) -> str:
    """Read project_name from config/harness.yaml, fall back to folder name."""
    try:
        import yaml  # noqa: PLC0415
        cfg_path = project_root / "config" / "harness.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            name = (cfg.get("slack") or {}).get("project_name", "")
            if name:
                return name
    except Exception:
        pass
    return project_root.name


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace, project_root: Path) -> int:
    from harness.runner import run_agent  # noqa: PLC0415
    from harness.state import is_known_agent, resolve_agent  # noqa: PLC0415

    agent = resolve_agent(args.agent)
    if not is_known_agent(agent):
        print(f"Error: unknown agent '{args.agent}'", file=sys.stderr)
        print("Run 'python -m harness list' to see available agents.", file=sys.stderr)
        return 1

    provider = _resolve_provider(args, project_root)

    print(f"Agent:    {agent}")
    print(f"Provider: {provider}")
    task_preview = args.task[:80] + ("..." if len(args.task) > 80 else "")
    print(f"Task:     {task_preview}")
    print()

    try:
        status_block, _ = run_agent(
            agent_name=agent,
            task=args.task,
            provider_name=provider,
            project_root=project_root,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Status:  {status_block.status.value}")
    if status_block.summary:
        print(f"Summary: {status_block.summary}")
    if status_block.next_agent:
        print(f"Next agent:  {status_block.next_agent}")
    if status_block.next_recommended_action:
        print(f"Next action: {status_block.next_recommended_action}")

    return 0 if status_block.status.value == "SUCCESS" else 1


def _cmd_status(args: argparse.Namespace, project_root: Path) -> int:
    latest = project_root / "context" / "latest.md"
    if not latest.exists():
        print("No runs yet. Run an agent first with: python -m harness run --agent <name> --task <task>")
        return 0
    print(latest.read_text(encoding="utf-8"))
    return 0


def _cmd_pipeline(args: argparse.Namespace, project_root: Path) -> int:
    from harness.pipeline import (  # noqa: PLC0415
        start_pipeline,
        resume_pipeline,
        list_pipelines,
        PipelineState,
    )
    from harness.state import is_known_agent, resolve_agent  # noqa: PLC0415

    # ── --list ────────────────────────────────────────────────────────────────
    if args.pipeline_list:
        states = list_pipelines(project_root / "runs")
        if not states:
            print("No pipelines found.")
            return 0
        header = f"{'Run ID':<38}  {'Started':<20}  {'Status':<10}  {'Current/Last Agent'}"
        print(header)
        print("-" * len(header))
        for s in states:
            started = s.created_at[:16].replace("T", " ")
            agent = s.current_agent or (s.completed[-1]["agent"] if s.completed else "-")
            print(f"{s.run_id:<38}  {started:<20}  {s.status:<10}  {agent}")
        return 0

    # ── --resume ──────────────────────────────────────────────────────────────
    if args.pipeline_resume:
        from harness.pipeline import load_state  # noqa: PLC0415

        run_id = args.pipeline_resume
        provider_override = getattr(args, "provider", None) or None

        # Peek at state to check if user input is needed before resuming
        user_input: str | None = None
        try:
            peek = load_state(run_id, project_root / "runs")
        except FileNotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if peek.pause_substate == "needs_user_input":
            print("Pipeline paused — agent needs your input:")
            print()
            print(peek.pause_reason)
            print()
            print("Your response (Enter = newline, Ctrl+D = submit):")
            lines: list[str] = []
            try:
                while True:
                    lines.append(input())
            except EOFError:
                pass
            except KeyboardInterrupt:
                print("\nCancelled.")
                return 1
            user_input = "\n".join(lines).strip() or None

        print(f"Resuming pipeline: {run_id}")
        print()
        try:
            state = resume_pipeline(
                run_id=run_id,
                project_root=project_root,
                provider=provider_override,
                user_input=user_input,
                converse=not getattr(args, "no_converse", False),
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        _print_pipeline_result(state, run_id)
        return 0 if state.status in ("complete", "paused") else 1

    # ── --start ───────────────────────────────────────────────────────────────
    start_agent = resolve_agent(args.pipeline_start) if args.pipeline_start else None
    if not start_agent:
        print("Error: --start <agent> is required to begin a pipeline.", file=sys.stderr)
        return 1
    if not args.task:
        print("Error: --task <text> is required when starting a pipeline.", file=sys.stderr)
        return 1
    if not is_known_agent(start_agent):
        print(f"Error: unknown agent '{args.pipeline_start}'", file=sys.stderr)
        print("Run 'python -m harness list' to see available agents.", file=sys.stderr)
        return 1

    provider = _resolve_provider(args, project_root)
    task_preview = args.task[:80] + ("..." if len(args.task) > 80 else "")
    print(f"Starting pipeline from:  {start_agent}")
    print(f"Provider:                {provider}")
    print(f"Task:                    {task_preview}")
    print()

    try:
        state = start_pipeline(
            start_agent=start_agent,
            task=args.task,
            provider=provider,
            project_root=project_root,
            converse=not getattr(args, "no_converse", False),
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    _print_pipeline_result(state, state.run_id)
    return 0 if state.status in ("complete", "paused") else 1


def _print_pipeline_result(state, run_id: str) -> None:
    """Print a clean summary after a pipeline run or resume."""
    print()
    print("─" * 60)
    print(f"Run ID:  {run_id}")
    print(f"Status:  {state.status.upper()}")

    if state.completed:
        print()
        print("Completed steps:")
        for step in state.completed:
            mark = "+" if step["status"] == "SUCCESS" else "!"
            print(f"  [{mark}] step {step['step']}  {step['agent']:<22}  {step['status']}")

    if state.status == "paused":
        substate = getattr(state, "pause_substate", "") or ""
        substate_label = {
            "waiting_for_human": "WAITING FOR HUMAN REVIEW",
            "needs_user_input": "NEEDS USER INPUT",
        }.get(substate, "PAUSED")
        print()
        print(f"PIPELINE {substate_label}")
        print()
        print(state.pause_reason)
        print()
        print(f"Next agent:  {state.current_agent}")
        print()
        print("When ready, resume with:")
        print(f"  python -m harness pipeline --resume {run_id}")

    elif state.status == "complete":
        print()
        print("Pipeline complete.")

    elif state.status == "escalated":
        print()
        print("PIPELINE ESCALATED — max attempts exceeded")
        print(f"Agent:   {state.failed_agent}")
        print(f"Reason:  {state.failed_reason}")
        if state.escalation_report_path:
            print(f"Report:  {state.escalation_report_path}")
        print()
        print("Review the escalation report and resume manually when ready.")

    elif state.status in ("failed", "blocked"):
        print()
        print(f"Pipeline stopped ({state.status.upper()})")
        print(f"Agent:   {state.failed_agent}")
        print(f"Reason:  {state.failed_reason}")

    print("─" * 60)


def _cmd_list(args: argparse.Namespace, project_root: Path) -> int:
    from harness.registry import list_agents, resolve_agents_dir  # noqa: PLC0415

    agents_dir = resolve_agents_dir(project_root)
    if not agents_dir.exists():
        print("No agents directory found.", file=sys.stderr)
        return 1

    agents = list_agents(agents_dir)
    if not agents:
        print("No agent specs found in agents/")
        return 1

    print("Available agents:")
    for name in agents:
        print(f"  {name}")
    return 0


def _cmd_init(args: argparse.Namespace, project_root: Path) -> int:
    dirs = ["context", "specs", "runs", "logs", "cache", "config"]
    for d in dirs:
        path = project_root / d
        path.mkdir(parents=True, exist_ok=True)
        print(f"  {'created' if not path.exists() else 'ok'}:  {d}/")

    templates: dict[str, str] = {
        "context/project.md": _PROJECT_MD,
        "context/latest.md": _LATEST_MD,
        "context/progress.md": _PROGRESS_MD,
        "specs/prd.md": _PRD_MD,
        "specs/tech-design.md": _TECH_DESIGN_MD,
        "config/harness.yaml": _HARNESS_YAML,
    }
    for rel, content in templates.items():
        path = project_root / rel
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            print(f"  created: {rel}")
        else:
            print(f"  exists:  {rel} (not overwritten)")

    print("\nProject initialized.")
    return 0


def _cmd_session(args: argparse.Namespace, project_root: Path) -> int:
    from harness.runner import run_agent  # noqa: PLC0415
    from harness.state import is_known_agent, resolve_agent  # noqa: PLC0415

    current_agent = resolve_agent(args.session_start)
    provider = _resolve_provider(args, project_root)
    project_name = _get_project_name(project_root)

    if not is_known_agent(current_agent):
        print(f"Error: unknown agent '{args.session_start}'", file=sys.stderr)
        print("Run 'harness list' to see available agents.", file=sys.stderr)
        return 1

    # Suppress INFO logs — only show warnings and errors
    logging.getLogger().setLevel(logging.WARNING)

    print(f"[{project_name}] {current_agent}  (type 'exit' to quit, 'switch <agent>' to change agent)")
    print()

    history: list[dict] = []

    while True:
        # ── Read input — Enter = newline, Ctrl+D = submit, Ctrl+C = exit ─────
        print(f"[{current_agent}]", end=" ", flush=True)
        lines: list[str] = []
        try:
            while True:
                line = input()
                stripped = line.strip().lower()

                if stripped in ("exit", "quit") and not lines:
                    print("\nBye.")
                    return 0

                if stripped.startswith("switch ") and not lines:
                    new_agent = resolve_agent(line.strip()[7:].strip())
                    if not is_known_agent(new_agent):
                        print(f"Unknown agent '{line.strip()[7:].strip()}'. Run 'harness list' to see available agents.")
                        break
                    current_agent = new_agent
                    print(f"→ Switched to {current_agent}")
                    break

                lines.append(line)
        except EOFError:
            if not lines:
                # Ctrl+D on empty prompt or stdin exhausted — exit
                print("\nBye.")
                return 0
            # Ctrl+D with content — fall through to submit
        except KeyboardInterrupt:
            print("\nBye.")
            return 0

        task = "\n".join(lines).strip()
        if not task:
            continue

        # ── Run agent (streaming) ─────────────────────────────────────────────
        print()

        # Filter: buffer tokens and stop printing when status block starts.
        # The status block is always the last ```json ... ``` in the response.
        _buf = ""
        _stopped = False
        _MARKER = "```json"

        def _on_token(token: str) -> None:
            nonlocal _buf, _stopped
            if _stopped:
                return
            _buf += token
            if _MARKER in _buf:
                # Print everything before the marker, then stop
                before = _buf[: _buf.index(_MARKER)].rstrip()
                if before:
                    sys.stdout.write(before)
                    sys.stdout.flush()
                _stopped = True
                return
            # Safe to flush up to len(buf) - len(MARKER) chars
            safe = max(0, len(_buf) - len(_MARKER))
            if safe:
                sys.stdout.write(_buf[:safe])
                sys.stdout.flush()
                _buf = _buf[safe:]

        try:
            status_block, response_text = run_agent(
                agent_name=current_agent,
                task=task,
                provider_name=provider,
                project_root=project_root,
                stream_callback=_on_token,
                history=history,
            )
        except KeyboardInterrupt:
            print("\nBye.")
            return 0
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            print()
            continue

        # Flush any remaining buffered text if marker was never seen
        if not _stopped and _buf.strip():
            sys.stdout.write(_buf)
            sys.stdout.flush()

        if status_block.status.value in ("FAILED", "BLOCKED"):
            print(f"\n[{status_block.status.value}]", file=sys.stderr)

        print()

        # Update conversation history for next turn
        history.append({"role": "user", "content": task})
        if response_text:
            history.append({"role": "assistant", "content": response_text})


def _cmd_chat(args: argparse.Namespace, project_root: Path) -> int:
    """Free-form conversation with the advisor agent, with optional pipeline handoff."""
    from harness.conversation import enter_conversation_loop  # noqa: PLC0415
    from harness.memory import ProjectMemory                  # noqa: PLC0415
    from harness.pipeline import start_pipeline               # noqa: PLC0415
    from harness.status import StatusCode, generate_run_id    # noqa: PLC0415

    task = getattr(args, "task", None) or ""
    provider = _resolve_provider(args, project_root)
    no_pipeline = getattr(args, "no_pipeline", False)

    # Inject cross-session memory so advisor has project context
    memories = ProjectMemory.load(project_root)
    if memories:
        task = task + "\n\n---\n\n" + ProjectMemory.to_prompt_section(memories)

    run_id = generate_run_id()

    print(f"\n\033[1;35m[advisor]\033[0m  provider={provider}")
    print("Type your thoughts. 'exit' or Ctrl+D to quit.\n")

    final_block = enter_conversation_loop(
        agent_name="advisor",
        task=task,
        provider_name=provider,
        project_root=project_root,
        run_id=run_id,
        initial_summary="",
    )

    # Pipeline handoff
    next_agent = (final_block.next_agent or "").strip()
    if (
        not no_pipeline
        and final_block.status == StatusCode.SUCCESS
        and next_agent
    ):
        pipeline_task = _build_pipeline_task_from_advisor(final_block)
        print(f"\n\033[1;32m[chat → pipeline]\033[0m  starting from {next_agent}\n")
        state = start_pipeline(
            start_agent=next_agent,
            task=pipeline_task,
            provider=provider,
            project_root=project_root,
        )
        _print_pipeline_result(state)
        return 0 if state.status == "complete" else 1

    return 0


def _build_pipeline_task_from_advisor(block) -> str:
    """Construct the pipeline task from advisor's final status block."""
    parts = [block.summary or "Build the product discussed in the conversation."]
    if block.handoff:
        h = block.handoff
        if h.decisions:
            parts.append("Key decisions: " + "; ".join(h.decisions))
        if h.requirements:
            parts.append("Requirements: " + "; ".join(h.requirements))
        if h.notes:
            parts.append(h.notes)
    return "\n\n".join(parts)


def _print_pipeline_result(state) -> None:
    symbols = {"complete": "✓", "failed": "✗", "blocked": "⊘",
                "escalated": "⚠", "paused": "⏸"}
    sym = symbols.get(state.status, "?")
    print(f"\033[1;35m[pipeline]\033[0m  {sym} status={state.status}  run_id={state.run_id}")
    if getattr(state, "failed_reason", ""):
        print(f"\033[1;35m[pipeline]\033[0m  reason: {state.failed_reason}")


def _cmd_ralph(args: argparse.Namespace, project_root: Path) -> int:
    """Run the SE↔QA tight loop (Ralph mode)."""
    from harness.ralph import run_ralph_loop  # noqa: PLC0415

    task = getattr(args, "task", None)
    if not task:
        task = _read_task_interactive("Ralph mode: SE↔QA tight loop.")
    if not task:
        print("No task provided.", file=sys.stderr)
        return 1

    provider = _resolve_provider(args, project_root)
    max_iter = getattr(args, "max_iterations", 5)

    project_name = _get_project_name(project_root)
    print(f"\n[ralph] project={project_name}  provider={provider}  max_iterations={max_iter}")
    print(f"[ralph] task: {task[:120]}{'...' if len(task) > 120 else ''}\n")

    state = run_ralph_loop(
        task=task,
        provider=provider,
        project_root=project_root,
        max_iterations=max_iter,
    )

    status_symbols = {
        "complete": "✓",
        "failed": "✗",
        "blocked": "⊘",
        "escalated": "⚠",
    }
    symbol = status_symbols.get(state.status, "?")
    print(f"\n[ralph] {symbol} status={state.status}  run_id={state.run_id}")
    print(f"[ralph] steps completed: {len(state.completed)}")
    if state.failed_reason:
        print(f"[ralph] reason: {state.failed_reason}")

    return 0 if state.status == "complete" else 1


def _cmd_slack_setup(args: argparse.Namespace, project_root: Path) -> int:
    """Interactive Slack integration setup — updates config/harness.yaml slack section."""
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        print("Error: pyyaml is required. Run: pip install pyyaml", file=sys.stderr)
        return 1

    cfg_path = project_root / "config" / "harness.yaml"
    cfg: dict = {}
    if cfg_path.exists():
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            print(f"Warning: could not parse existing config: {exc}", file=sys.stderr)

    existing_slack = cfg.get("slack", {}) or {}

    print("=" * 60)
    print("Harness Slack Setup")
    print("=" * 60)
    print()
    print("This wizard configures which Slack channels harness posts to.")
    print("Credentials (webhook URL / bot token) are read from env vars")
    print("at runtime — never stored in config files.")
    print()

    def prompt(label: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        val = input(f"{label}{hint}: ").strip()
        return val if val else default

    def prompt_bool(label: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        val = input(f"{label} [{hint}]: ").strip().lower()
        if not val:
            return default
        return val in ("y", "yes", "1", "true")

    # ── Gather inputs ─────────────────────────────────────────────────────────
    project_name = prompt(
        "Project name (used for #proj-<name> channel suggestion)",
        existing_slack.get("project_name", ""),
    )

    suggested_project_channel = f"proj-{project_name}" if project_name else "proj-myapp"
    project_channel = prompt(
        "Project Slack channel (e.g. proj-myapp)",
        existing_slack.get("project_channel", suggested_project_channel),
    )

    control_channel = prompt(
        "Control/alerts channel (e.g. ai-harness-control)",
        existing_slack.get("control_channel", "ai-harness-control"),
    )

    notify_control = prompt_bool(
        "Also post high-level alerts to control channel?",
        existing_slack.get("notify_control_channel", True),
    )

    print()
    print("Env var names for credentials (leave blank for defaults):")
    webhook_env = prompt(
        "  Env var for Slack Webhook URL",
        existing_slack.get("webhook_url_env", "SLACK_WEBHOOK_URL"),
    ) or "SLACK_WEBHOOK_URL"

    bot_token_env = prompt(
        "  Env var for Slack Bot Token",
        existing_slack.get("bot_token_env", "SLACK_BOT_TOKEN"),
    ) or "SLACK_BOT_TOKEN"

    # ── Write updated config ──────────────────────────────────────────────────
    cfg["slack"] = {
        "enabled": True,
        "project_name": project_name,
        "project_channel": project_channel,
        "control_channel": control_channel,
        "notify_control_channel": notify_control,
        "webhook_url_env": webhook_env,
        "bot_token_env": bot_token_env,
    }

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    print()
    print(f"  Saved: {cfg_path.relative_to(project_root)}")

    # ── Print setup instructions ──────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Next steps to complete Slack integration")
    print("=" * 60)
    print()
    print("Option A — Incoming Webhook (simplest, no threading):")
    print("  1. Go to https://api.slack.com/apps → Create New App")
    print("     → 'From scratch' → name it, pick your workspace")
    print("  2. Features → Incoming Webhooks → Activate")
    print("     → 'Add New Webhook to Workspace' → pick channel")
    print("  3. Copy the Webhook URL and export it:")
    print(f"       export {webhook_env}=https://hooks.slack.com/services/...")
    print()
    print("Option B — Bot Token (recommended, enables threading):")
    print("  1. Go to https://api.slack.com/apps → Create New App")
    print("     → 'From scratch' → name it, pick your workspace")
    print("  2. OAuth & Permissions → Bot Token Scopes → Add:")
    print("       chat:write")
    print("       chat:write.public  (if posting to channels bot isn't in)")
    print("  3. Install to Workspace → copy Bot User OAuth Token")
    print(f"       export {bot_token_env}=xoxb-...")
    print("  4. Invite the bot to your channels:")
    print(f"       /invite @yourbot  (in #{project_channel})")
    if notify_control:
        print(f"       /invite @yourbot  (in #{control_channel})")
    print()
    print("Then run a pipeline — harness will post automatically.")
    print()

    return 0


# ---------------------------------------------------------------------------
# Template content for `init`
# ---------------------------------------------------------------------------

_PROJECT_MD = """\
# Project Context

## Project Summary

TBD

## Target Users

TBD

## Product Purpose

TBD

## Core Features

TBD

## Current Tech Stack Summary

TBD

## Current Architecture Summary

TBD

## Important Project-Level Principles

TBD

## Major Decisions

TBD
"""

_LATEST_MD = """\
# Latest Change

## Timestamp

TBD

## Run ID

TBD

## Task ID

TBD

## Agent

TBD

## Summary

TBD

## Files Changed

TBD

## Result Status

TBD

## Next Recommended Action

TBD
"""

_PROGRESS_MD = """\
# Progress

Newest entry first. Keep latest 15 entries only.
"""

_PRD_MD = """\
# PRD

Only the human user may manually edit this file.

Agents may read this file but must not create, modify, overwrite, append, or delete it.

## Product Summary

TBD

## Target User

TBD

## Problem

TBD

## Goal

TBD

## Non-Goals

TBD

## User Stories

TBD

## Core User Flows

TBD

## P0 Scope

TBD

## P1 Scope

TBD

## P2 Scope

TBD

## Acceptance Criteria

TBD

## Open Questions

TBD

## Assumptions

TBD

## Risks

TBD

## Out of Scope

TBD
"""

_HARNESS_YAML = """\
# Harness project configuration
# Credentials (API keys, tokens, webhook URLs) go in env vars — never in this file.

# Which LLM provider to use by default.
# Supported: openrouter | stub
default_provider: openrouter

# Slack integration (optional — outbound-only notifications)
slack:
  # Set to true once you have run 'harness slack-setup' and exported credentials.
  enabled: false

  # Short project identifier used in messages.
  project_name: ""

  # Channel for detailed per-agent updates (e.g. proj-myapp).
  project_channel: ""

  # Channel for high-level pipeline alerts (e.g. ai-harness-control).
  control_channel: ai-harness-control

  # Whether to also send summary events to the control channel.
  notify_control_channel: true

  # Name of the env var holding the Slack Incoming Webhook URL.
  # Bot Token (below) takes priority when both are set.
  webhook_url_env: SLACK_WEBHOOK_URL

  # Name of the env var holding the Slack Bot User OAuth Token (xoxb-...).
  # Required for threaded replies. Needs chat:write scope.
  bot_token_env: SLACK_BOT_TOKEN

# Memory backend for cross-session project knowledge.
# Options:
#   json  (default) — stored in context/memory.json, no external dependencies
#   mem0            — local Qdrant vector DB + OpenAI embedder
#                     requires: pip install mem0ai + OPENAI_API_KEY env var
#                     data stored in <project_root>/.mem0/
memory:
  backend: json
"""

_TECH_DESIGN_MD = """\
# Technical Design

Only the system_architect agent may create or modify this file.

Other agents may read this file but must not modify it.

## Overview

TBD

## Architecture

TBD

## Key Components

TBD

## Data Models

TBD

## API Contracts

TBD

## Technology Decisions

TBD

## Non-Functional Requirements

TBD

## Open Questions

TBD
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="AI multi-agent workflow harness",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--root",
        default=None,
        metavar="DIR",
        help="Project root directory (default: auto-detect from config/harness.yaml)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # run
    run_p = sub.add_parser("run", help="Run an agent")
    run_p.add_argument("--agent", "-a", required=True, metavar="NAME", help="Agent name")
    run_p.add_argument("--task", "-t", required=True, metavar="TEXT", help="Task prompt")
    run_p.add_argument(
        "--provider", "-p",
        metavar="NAME",
        help="Provider: openrouter (default) or stub",
    )

    # status
    sub.add_parser("status", help="Show the latest run status")

    # list
    sub.add_parser("list", help="List available agents")

    # init
    sub.add_parser("init", help="Initialise project directories and seed template files")

    # session
    session_p = sub.add_parser("session", help="Start an interactive session (run multiple tasks in a loop)")
    session_p.add_argument(
        "--start", "-a",
        required=True,
        metavar="AGENT",
        dest="session_start",
        help="Starting agent for each pipeline run",
    )
    session_p.add_argument("--provider", "-p", metavar="NAME", help="Provider override")

    # slack-setup
    sub.add_parser("slack-setup", help="Interactive Slack channel configuration wizard")

    # chat
    chat_p = sub.add_parser(
        "chat",
        help="Free-form conversation with advisor — discuss ideas, then optionally start pipeline",
    )
    chat_p.add_argument("--task", "-t", metavar="TEXT",
                        help="Opening topic or context (optional)")
    chat_p.add_argument("--provider", "-p", metavar="NAME", help="Provider override")
    chat_p.add_argument("--no-pipeline", action="store_true", dest="no_pipeline",
                        help="End chat without starting a pipeline even if advisor suggests one")

    # ralph
    ralph_p = sub.add_parser(
        "ralph",
        help="SE↔QA tight loop — implement then verify, repeat until QA passes",
    )
    ralph_p.add_argument("--task", "-t", metavar="TEXT", help="Task prompt")
    ralph_p.add_argument("--provider", "-p", metavar="NAME", help="Provider override")
    ralph_p.add_argument(
        "--max-iterations", "-n",
        type=int, default=5, dest="max_iterations",
        metavar="N",
        help="Maximum SE→QA iterations (default: 5)",
    )

    # slack-listen
    sub.add_parser(
        "slack-listen",
        help="Start the Slack bot (remote control via DM: 'pipeline: <task>', 'run <agent>: <task>', 'status')",
    )

    # pipeline
    pipe_p = sub.add_parser("pipeline", help="Run or manage multi-agent pipelines")
    pipe_mode = pipe_p.add_mutually_exclusive_group(required=True)
    pipe_mode.add_argument(
        "--start",
        metavar="AGENT",
        dest="pipeline_start",
        help="Start a new pipeline from this agent",
    )
    pipe_mode.add_argument(
        "--resume",
        metavar="RUN_ID",
        dest="pipeline_resume",
        help="Resume a paused pipeline",
    )
    pipe_mode.add_argument(
        "--list",
        action="store_true",
        dest="pipeline_list",
        help="List recent pipelines",
    )
    pipe_p.add_argument("--task", "-t", metavar="TEXT", help="Task prompt (required with --start)")
    pipe_p.add_argument("--provider", "-p", metavar="NAME", help="Provider override")
    pipe_p.add_argument(
        "--no-converse",
        action="store_true",
        dest="no_converse",
        help="Disable conversation mode: pause on NEEDS_USER_INPUT instead of entering interactive loop",
    )

    args = parser.parse_args()
    _configure_logging(args.verbose)

    project_root = Path(args.root).resolve() if args.root else _find_project_root()

    try:
        if args.command == "run":
            sys.exit(_cmd_run(args, project_root))
        elif args.command == "status":
            sys.exit(_cmd_status(args, project_root))
        elif args.command == "list":
            sys.exit(_cmd_list(args, project_root))
        elif args.command == "init":
            sys.exit(_cmd_init(args, project_root))
        elif args.command == "pipeline":
            sys.exit(_cmd_pipeline(args, project_root))
        elif args.command == "session":
            sys.exit(_cmd_session(args, project_root))
        elif args.command == "chat":
            sys.exit(_cmd_chat(args, project_root))
        elif args.command == "ralph":
            sys.exit(_cmd_ralph(args, project_root))
        elif args.command == "slack-setup":
            sys.exit(_cmd_slack_setup(args, project_root))
        elif args.command == "slack-listen":
            from harness.integrations.slack_bot import run_bot  # noqa: PLC0415
            try:
                run_bot(project_root)
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            sys.exit(0)
        else:
            parser.print_help()
            sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
