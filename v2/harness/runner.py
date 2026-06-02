from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.context import RunContext

from harness.hooks import (
    AFTER_AGENT_RUN,
    AFTER_PROVIDER_CALL,
    AFTER_TOOL_EXECUTION,
    BEFORE_AGENT_RUN,
    BEFORE_PROVIDER_CALL,
    BEFORE_TOOL_EXECUTION,
    ON_ERROR,
    ON_STATUS_CHANGE,
    HookSystem,
    hooks as default_hooks,
)
from harness.models.config import load_model_config
from harness.providers.base import ProviderResult
from harness.providers.openrouter import OpenRouterProvider
from harness.providers.stub import StubProvider
from harness.registry import AgentSpec, load_agent, resolve_agents_dir
from harness.report import (
    build_report_markdown,
    parse_status_block,
    save_run_artifacts,
    update_latest,
    update_progress,
    write_run_report,
    write_specs_report,
)
from harness.status import (
    HandoffNote,
    IssueItem,
    StatusBlock,
    StatusCode,
    generate_run_id,
    generate_task_id,
)
from harness.state import is_known_agent
from harness.tools.budget import ToolBudget
from harness.tools.dispatcher import (
    dispatch_tool_requests,
    format_tool_results_for_prompt,
    parse_tool_requests,
)
from harness.tools.mutations import MutationContext

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hallucination detection
# ---------------------------------------------------------------------------

def _validate_claimed_files(response_text: str, project_root: Path) -> list[str]:
    """
    Parse the status block from a response and check that every file listed
    in files_created actually exists on disk.

    Returns a list of human-readable error strings for missing files.
    Files in files_modified are checked only if they already existed before
    the run — here we just ensure they exist now.

    Skips report/run-artifact paths (runs/<run_id>/...) since those are
    written by the harness itself after this check runs.
    """
    parsed = parse_status_block(response_text)
    if not parsed:
        return []

    errors: list[str] = []
    for rel_path in parsed.get("files_created", []):
        # Skip harness-managed run artifacts
        if rel_path.startswith("runs/") or rel_path.startswith("specs/"):
            continue
        if not (project_root / rel_path).exists():
            errors.append(
                f"files_created: '{rel_path}' — file does not exist on disk. "
                "You MUST issue a WRITE_FILE tool_request to create it. "
                "Listing it in files_created without creating it is a hallucination."
            )
    return errors


def _format_hallucination_feedback(errors: list[str]) -> str:
    """Format hallucination errors as a tool-result style message for re-injection."""
    lines = [
        "## Harness Validation Error — Hallucinated Files Detected",
        "",
        "The following files you listed in `files_created` do not exist on disk.",
        "You must fix this before your response can be accepted.",
        "",
    ]
    for i, err in enumerate(errors, 1):
        lines.append(f"{i}. {err}")
    lines += [
        "",
        "**Required actions:**",
        "- Issue a `WRITE_FILE` tool_request for each missing file.",
        "- After creating the files, re-emit your final status block.",
        "- Do NOT list a file in `files_created` unless you actually created it via WRITE_FILE.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------

def _append_usage_log(
    project_root: Path,
    agent_name: str,
    resolved_model: str,
    usage: dict | None,
    timestamp: str,
) -> None:
    """Append one usage entry to logs/usage.jsonl for dashboard ingestion."""
    usage = usage or {}
    project = project_root.name
    try:
        import yaml  # noqa: PLC0415
        cfg_path = project_root / "config" / "harness.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            project = cfg.get("project") or cfg.get("slack", {}).get("project_name") or project
    except Exception:
        pass

    entry = {
        "timestamp": timestamp,
        "project": project,
        "stage": agent_name,
        "model": resolved_model,
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "cost_usd": usage.get("cost", 0.0),
    }
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "usage.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    log.info(
        "Usage logged: agent=%s model=%s in=%d out=%d cost=$%.5f",
        agent_name, resolved_model,
        entry["input_tokens"], entry["output_tokens"], entry["cost_usd"],
    )


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def _get_provider(name: str):
    if name == "stub":
        return StubProvider()
    if name == "openrouter":
        return OpenRouterProvider()
    raise ValueError(f"Unknown provider: {name!r}. Use 'openrouter' or 'stub'.")


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _load_context_files(
    context_files: list[str],
    project_root: Path,
) -> tuple[dict[str, str], list[str]]:
    """Return (loaded_map, skipped_paths)."""
    loaded: dict[str, str] = {}
    skipped: list[str] = []
    for rel_path in context_files:
        full_path = project_root / rel_path
        if full_path.exists():
            loaded[rel_path] = full_path.read_text(encoding="utf-8")
        else:
            log.warning("Context file not found (skipping): %s", rel_path)
            skipped.append(rel_path)
    return loaded, skipped


def _build_prompt_package(
    spec: AgentSpec,
    task: str,
    run_id: str,
    task_id: str,
    context_contents: dict[str, str],
    run_context: "RunContext | None" = None,
) -> dict:
    parts: list[str] = [f"## Task\n\n{task}"]

    # RunContext injected before static context files so agents see shared
    # pipeline state (files created, decisions) before reading disk snapshots.
    if run_context and not run_context.is_empty():
        parts.append(run_context.to_prompt_section())

    if context_contents:
        ctx_parts = [
            f"### {path}\n\n{content}"
            for path, content in context_contents.items()
        ]
        parts.append("## Context Files\n\n" + "\n\n---\n\n".join(ctx_parts))

    parts.append(f"## Run Metadata\n\nrun_id: {run_id}\ntask_id: {task_id}")

    return {
        "agent_name": spec.name,
        "system_prompt": spec.instructions,
        "user_message": "\n\n---\n\n".join(parts),
        "run_id": run_id,
        "task_id": task_id,
    }


# ---------------------------------------------------------------------------
# Status mapping  (provider result → agent StatusBlock)
# ---------------------------------------------------------------------------

def _map_provider_result(
    result: ProviderResult,
    run_id: str,
    task_id: str,
    agent_name: str,
    default_next_agent: str,
) -> tuple[StatusBlock, str]:
    """
    Map a ProviderResult to an agent StatusBlock.

    Provider status ("success" | "error") is intentionally separate from
    agent run status (SUCCESS | FAILED | BLOCKED | …).

    If the API key is missing → BLOCKED.
    Other provider errors     → FAILED.
    Success with parseable status block → use the parsed block.
    Success without status block → default to SUCCESS.
    """
    if result.status == "error":
        # Use a specific sentinel rather than a fragile substring match.
        # OpenRouterProvider sets this exact error when the env var is absent.
        _MISSING_KEY_ERROR = "OPENROUTER_API_KEY is not set"
        if result.error and result.error.startswith(_MISSING_KEY_ERROR):
            agent_status = StatusCode.BLOCKED
            summary = f"Agent blocked: {result.error}"
        else:
            agent_status = StatusCode.FAILED
            summary = f"Provider error: {result.error}"

        block = StatusBlock(
            run_id=run_id,
            task_id=task_id,
            agent=agent_name,
            status=agent_status,
            summary=summary,
            issues_found=[result.error or "Unknown provider error"],
            next_recommended_action=(
                "Set OPENROUTER_API_KEY or use --provider stub."
                if agent_status == StatusCode.BLOCKED
                else "Check provider logs and retry."
            ),
        )
        return block, ""

    # Provider returned text — try to extract the structured status block.
    parsed = parse_status_block(result.text)
    if parsed:
        try:
            raw_handoff = parsed.get("handoff")
            handoff = HandoffNote.from_dict(raw_handoff) if isinstance(raw_handoff, dict) else None

            # Parse structured issues list (QA/reviewer → issue-resolution loop)
            raw_issues_list = parsed.get("issues_list", [])
            issues_list: list[IssueItem] = []
            for i, raw in enumerate(raw_issues_list):
                if isinstance(raw, dict):
                    item = IssueItem.from_dict(raw)
                    # Auto-assign id if agent didn't provide one
                    if not item.id:
                        item.id = f"issue_{i + 1}"
                    if not item.found_by:
                        item.found_by = agent_name
                    issues_list.append(item)

            block = StatusBlock(
                # run_id and task_id are always authoritative from the harness —
                # never trust what the LLM returns here; it may be stale or wrong.
                run_id=run_id,
                task_id=task_id,
                agent=agent_name,
                status=StatusCode(parsed.get("status", "SUCCESS")),
                summary=parsed.get("summary", ""),
                files_requested=parsed.get("files_requested", []),
                files_created=parsed.get("files_created", []),
                files_modified=parsed.get("files_modified", []),
                files_deleted=parsed.get("files_deleted", []),
                commands_run=parsed.get("commands_run", []),
                command_results=parsed.get("command_results", []),
                issues_found=parsed.get("issues_found", []),
                human_input_required=bool(parsed.get("human_input_required", False)),
                next_recommended_action=parsed.get("next_recommended_action", ""),
                next_agent=parsed.get("next_agent", default_next_agent),
                handoff=handoff,
                issues_list=issues_list,
            )
            return block, result.text
        except (ValueError, KeyError) as exc:
            log.warning("Status block fields invalid (%s) — falling back to SUCCESS", exc)

    # Fallback: provider gave text but no parseable block.
    block = StatusBlock(
        run_id=run_id,
        task_id=task_id,
        agent=agent_name,
        status=StatusCode.SUCCESS,
        summary="Agent completed (no structured status block found in output).",
        next_agent=default_next_agent,
    )
    return block, result.text


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_agent(
    agent_name: str,
    task: str,
    provider_name: str,
    project_root: Path,
    run_id: str | None = None,
    hook_system: HookSystem | None = None,
    stream_callback: "Callable[[str], None] | None" = None,
    history: "list[dict] | None" = None,
    run_context: "RunContext | None" = None,
) -> tuple["StatusBlock", str]:
    """
    Run a single agent and return its StatusBlock.

    run_id:
        If provided (by the pipeline runner), all agents share the same runs/<run_id>/
        directory. If omitted, a new run_id is generated (single-agent `harness run`).

    Side effects:
        - Writes runs/<run_id>/<report>.md
        - Updates context/latest.md
        - Updates context/progress.md
        - Emits lifecycle hooks
    """
    if not is_known_agent(agent_name):
        raise ValueError(
            f"Unknown agent: {agent_name!r}. "
            "Run 'python -m harness list' to see available agents."
        )

    hs = hook_system or default_hooks

    run_id = run_id or generate_run_id()
    task_id = generate_task_id(agent_name)

    agents_dir = resolve_agents_dir(project_root)
    config_dir = project_root / "config"
    runs_dir = project_root / "runs"
    context_dir = project_root / "context"

    spec: AgentSpec = load_agent(agent_name, agents_dir)
    model_cfg = load_model_config(config_dir / "models.yaml")
    resolved_model = model_cfg.resolve(spec.model.tier)

    model_config = {
        "provider": spec.model.provider,
        "tier": spec.model.tier,
        "model": resolved_model,
        "temperature": spec.model.temperature,
    }

    # ── before_agent_run ────────────────────────────────────────────────────
    hs.emit(BEFORE_AGENT_RUN, {
        "run_id": run_id,
        "task_id": task_id,
        "agent": agent_name,
        "task": task,
        "model": resolved_model,
        "provider": provider_name,
    })

    context_contents, skipped_context = _load_context_files(spec.context_files, project_root)
    prompt_package = _build_prompt_package(spec, task, run_id, task_id, context_contents, run_context)

    provider = _get_provider(provider_name)

    # ── Multi-turn tool loop ─────────────────────────────────────────────────
    # Each iteration: call provider → parse → budget-check → dispatch → inject.
    # Stops when: (a) no valid tool_requests, (b) provider error, (c) max turns,
    #             (d) budget exhausted, (e) too many malformed requests.
    MAX_TOOL_TURNS = 10
    budget = ToolBudget(max_tool_calls=spec.tool_budget)
    mutation_ctx = MutationContext(
        run_id=run_id,
        agent_name=agent_name,
        task_id=task_id,
        runs_dir=runs_dir,
        project_root=project_root,
        hook_system=hs,
    )
    tool_turn = 0
    total_latency_s = 0.0
    result: ProviderResult | None = None
    current_prompt = prompt_package
    # Accumulate token usage across all tool turns — each turn is a separate LLM call.
    _accumulated_usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}

    while tool_turn <= MAX_TOOL_TURNS:
        hs.emit(BEFORE_PROVIDER_CALL, {
            "run_id": run_id,
            "task_id": task_id,
            "agent": agent_name,
            "provider": provider_name,
            "model": resolved_model,
            "tool_turn": tool_turn,
        })

        _t0 = time.monotonic()
        # Stream and pass history only on the first turn (no tool results injected yet)
        _on_token = stream_callback if tool_turn == 0 else None
        _history = history if tool_turn == 0 else None
        result = provider.generate(current_prompt, model_config, on_token=_on_token, history=_history)
        if _on_token:
            # Newline after streamed output before anything else prints
            import sys as _sys; _sys.stdout.write("\n"); _sys.stdout.flush()
        turn_latency = time.monotonic() - _t0
        total_latency_s += turn_latency

        # Accumulate token/cost usage across every tool turn.
        if result.usage:
            _accumulated_usage["prompt_tokens"] += result.usage.get("prompt_tokens", 0)
            _accumulated_usage["completion_tokens"] += result.usage.get("completion_tokens", 0)
            _accumulated_usage["cost"] += result.usage.get("cost", 0.0)

        hs.emit(AFTER_PROVIDER_CALL, {
            "run_id": run_id,
            "task_id": task_id,
            "agent": agent_name,
            "provider": provider_name,
            "model": resolved_model,
            "provider_status": result.status,
            "error": result.error,
            "usage": result.usage,
            "tool_turn": tool_turn,
            "turn_latency_s": round(turn_latency, 3),
        })

        if result.status == "error":
            hs.emit(ON_ERROR, {
                "run_id": run_id,
                "task_id": task_id,
                "agent": agent_name,
                "provider": provider_name,
                "error": result.error,
            })
            break

        # ── Parse tool requests ───────────────────────────────────────────────
        parse_result = parse_tool_requests(result.text)

        # Count parse errors toward malformed budget
        for err in parse_result.errors:
            log.warning("Tool parse error (turn=%d): %s", tool_turn, err)
            budget.record_malformed()

        # No valid requests → agent wants to stop.
        # Before accepting the response, validate claimed files against disk.
        if not parse_result.requests:
            if parse_result.errors:
                log.warning(
                    "Agent %s turn %d: all tool requests were malformed — stopping loop",
                    agent_name, tool_turn,
                )

            if tool_turn < MAX_TOOL_TURNS:
                hallucinations = _validate_claimed_files(result.text, project_root)
                if hallucinations:
                    log.warning(
                        "Agent %s: hallucinated %d file(s) — injecting feedback, retrying",
                        agent_name, len(hallucinations),
                    )
                    feedback = _format_hallucination_feedback(hallucinations)
                    current_prompt = dict(current_prompt)
                    current_prompt["user_message"] = (
                        current_prompt["user_message"] + "\n\n---\n\n" + feedback
                    )
                    tool_turn += 1
                    continue

            break

        # Too many malformed → hard stop to avoid prompt-injection loops.
        # Still run hallucination detection: if the agent claimed files it never
        # created (because its WRITE_FILE calls were malformed), we must catch it.
        if budget.halted_by_malformed():
            log.warning(
                "Agent %s: halted after %d malformed requests",
                agent_name, budget.malformed_count,
            )
            if tool_turn < MAX_TOOL_TURNS:
                hallucinations = _validate_claimed_files(result.text, project_root)
                if hallucinations:
                    log.warning(
                        "Agent %s: hallucinated %d file(s) after malformed halt — injecting feedback",
                        agent_name, len(hallucinations),
                    )
                    feedback = _format_hallucination_feedback(hallucinations)
                    # Reset malformed counter so the next turn gets a fresh budget
                    budget.malformed_count = 0
                    current_prompt = dict(current_prompt)
                    current_prompt["user_message"] = (
                        current_prompt["user_message"] + "\n\n---\n\n" + feedback
                    )
                    tool_turn += 1
                    continue
            break

        if tool_turn == MAX_TOOL_TURNS:
            log.warning(
                "Agent %s reached MAX_TOOL_TURNS=%d — stopping tool loop",
                agent_name, MAX_TOOL_TURNS,
            )
            break

        log.info(
            "Agent %s tool turn %d: %d request(s)",
            agent_name, tool_turn + 1, len(parse_result.requests),
        )
        hs.emit(BEFORE_TOOL_EXECUTION, {
            "run_id": run_id,
            "task_id": task_id,
            "agent": agent_name,
            "tool_turn": tool_turn + 1,
            "requests": parse_result.requests,
        })

        tool_results = dispatch_tool_requests(
            parse_result.requests, project_root, budget, mutation_ctx
        )

        hs.emit(AFTER_TOOL_EXECUTION, {
            "run_id": run_id,
            "task_id": task_id,
            "agent": agent_name,
            "tool_turn": tool_turn + 1,
            "results": tool_results,
            "budget": budget.summary(),
        })

        # Merge parse errors into tool results so the agent can see what failed
        parse_error_results = [
            {"tool": "PARSE_ERROR", "status": "error", **err}
            for err in parse_result.errors
        ]
        all_results = parse_error_results + tool_results

        results_text = format_tool_results_for_prompt(all_results)
        current_prompt = dict(current_prompt)
        current_prompt["user_message"] = (
            current_prompt["user_message"] + "\n\n---\n\n" + results_text
        )
        tool_turn += 1

    latency_s = total_latency_s

    # ── Kill background processes started by this agent ───────────────────────
    if mutation_ctx and mutation_ctx.background_pids:
        import os, signal  # noqa: E401
        for pid in mutation_ctx.background_pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                log.info("Killed background process group pgid=%d (pid=%d)", os.getpgid(pid), pid)
            except (ProcessLookupError, PermissionError):
                pass  # already exited
        mutation_ctx.background_pids.clear()

    status_block, report_text = _map_provider_result(
        result, run_id, task_id, agent_name, spec.default_next_agent
    )

    # ── save inspectability artifacts ────────────────────────────────────────
    save_run_artifacts(
        run_id=run_id,
        agent_name=agent_name,
        runs_dir=runs_dir,
        agents_dir=agents_dir,
        prompt_package=prompt_package,
        provider_name=provider_name,
        resolved_model=resolved_model,
        latency_s=latency_s,
        usage=result.usage if result else {},
        provider_status=result.status if result else "error",
        provider_error=result.error if result else "No result from provider",
        tool_budget_summary=budget.summary(),
        tool_turns=tool_turn,
        skipped_context_files=skipped_context,
    )

    _append_usage_log(
        project_root=project_root,
        agent_name=agent_name,
        resolved_model=resolved_model,
        usage=_accumulated_usage,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    )

    report_content = build_report_markdown(
        agent_name=agent_name,
        task=task,
        provider_text=report_text,
        status_block=status_block,
    )

    write_run_report(run_id, agent_name, report_content, spec.report_filename, runs_dir)

    if spec.specs_report_path:
        write_specs_report(report_content, spec.specs_report_path, project_root)

    files_changed = (
        status_block.files_created
        + status_block.files_modified
        + [f"runs/{run_id}/{spec.report_filename}"]
    )

    context_dir.mkdir(parents=True, exist_ok=True)

    update_latest(
        run_id=run_id,
        task_id=task_id,
        agent_name=agent_name,
        summary=status_block.summary or f"{agent_name} run completed.",
        status=status_block.status,
        files_changed=files_changed,
        next_action=status_block.next_recommended_action,
        context_dir=context_dir,
    )

    update_progress(
        run_id=run_id,
        task_id=task_id,
        agent_name=agent_name,
        summary=status_block.summary or f"{agent_name} run completed.",
        status=status_block.status,
        next_action=status_block.next_recommended_action,
        context_dir=context_dir,
    )

    # ── on_status_change ────────────────────────────────────────────────────
    hs.emit(ON_STATUS_CHANGE, {
        "run_id": run_id,
        "task_id": task_id,
        "agent": agent_name,
        "status": status_block.status.value,
    })

    # ── after_agent_run ─────────────────────────────────────────────────────
    hs.emit(AFTER_AGENT_RUN, {
        "run_id": run_id,
        "task_id": task_id,
        "agent": agent_name,
        "status": status_block.status.value,
        "summary": status_block.summary,
        "next_agent": status_block.next_agent,
        "report_path": f"runs/{run_id}/{spec.report_filename}",
        "provider": provider_name,
        "model": resolved_model,
        "usage": result.usage,
    })

    log.info(
        "Agent %s finished | status=%s run_id=%s",
        agent_name,
        status_block.status.value,
        run_id,
    )
    return status_block, report_text
