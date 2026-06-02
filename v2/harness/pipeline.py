from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from harness.hooks import (
    HookSystem,
    ON_ESCALATION,
    ON_HUMAN_APPROVAL_REQUIRED,
    ON_MAX_ATTEMPTS_REACHED,
    ON_WORKFLOW_COMPLETE,
    ON_WORKFLOW_PAUSED,
    ON_WORKFLOW_RESUMED,
    hooks as default_hooks,
)
from harness.conversation import enter_conversation_loop
from harness.registry import load_agent, resolve_agents_dir
from harness.report import write_escalation_report
from harness.runner import run_agent
from harness.state import failure_is_recoverable, next_agent as state_next_agent, debugger_target
from harness.context import RunContext
from harness.memory import ProjectMemory
from harness.status import HandoffNote, StatusCode, generate_run_id

log = logging.getLogger(__name__)

_STATE_FILENAME = "pipeline.json"


# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------

@dataclass
class PipelineState:
    run_id: str
    task: str
    start_agent: str
    provider: str
    status: str         # running | paused | complete | failed | blocked | escalated
    current_agent: str  # next agent to run; "" when pipeline is done
    completed: list[dict] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    pause_reason: str = ""
    failed_agent: str = ""
    failed_reason: str = ""
    # Paused substate — set whenever status == "paused".
    #   "waiting_for_human"  — human gate after successful agent step
    #   "needs_user_input"   — agent explicitly requested more information
    #   ""                   — generic pause (backward-compat default)
    pause_substate: str = ""
    # Recovery tracking
    recovery_from: str = ""          # which agent originally failed (set when routing to recovery_engineer)
    debugger_from: str = ""          # which agent requested debugging (set when routing to debugger_engineer)
    attempt_counts: dict = field(default_factory=dict)   # {agent_name: int} — consecutive failure retries only
    feature_counts: dict = field(default_factory=dict)   # {agent_name: int} — successful feature completions (FEATURE_COMPLETE loops)
    escalation_report_path: str = "" # set when max_attempts exceeded
    # Slack threading — set when pipeline starts and bot token is available.
    # "" means webhook mode (no threading) or Slack not configured.
    slack_thread_ts: str = ""
    # Structured handoff emitted by the most recently completed agent.
    # Injected as a formatted section in the next agent's task prompt.
    last_handoff: dict | None = None
    # User inputs collected during NEEDS_USER_INPUT pauses.
    # Each entry: {at: iso_timestamp, requested_by: agent_name, input: str}
    user_inputs: list = field(default_factory=list)
    # Issues accumulated across all agents — injected into each subsequent agent's task.
    # Each entry: {agent: str, issues: list[str]}
    accumulated_issues: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineState":
        known = {
            "run_id", "task", "start_agent", "provider", "status",
            "current_agent", "completed", "created_at", "updated_at",
            "pause_reason", "failed_agent", "failed_reason",
            "pause_substate",
            "recovery_from", "debugger_from", "attempt_counts", "feature_counts", "escalation_report_path",
            "slack_thread_ts", "user_inputs", "accumulated_issues",
            "last_handoff",
        }
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / run_id / _STATE_FILENAME


def _save(state: PipelineState, runs_dir: Path) -> None:
    state.updated_at = _now_iso()
    path = _state_path(runs_dir, state.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    log.debug("Pipeline state saved  run_id=%s  status=%s", state.run_id, state.status)


def load_state(run_id: str, runs_dir: Path) -> PipelineState:
    path = _state_path(runs_dir, run_id)
    if not path.exists():
        raise FileNotFoundError(
            f"No pipeline state found for run_id {run_id!r}.\n"
            f"Expected: {path}\n"
            "Use 'python -m harness pipeline --list' to see available pipelines."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return PipelineState.from_dict(data)


def list_pipelines(runs_dir: Path) -> list[PipelineState]:
    if not runs_dir.exists():
        return []
    states: list[PipelineState] = []
    for run_dir in sorted(runs_dir.iterdir(), reverse=True):
        state_file = run_dir / _STATE_FILENAME
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                states.append(PipelineState.from_dict(data))
            except Exception as exc:
                log.warning("Could not read pipeline state %s: %s", state_file, exc)
    return states


# ---------------------------------------------------------------------------
# Gate check
# ---------------------------------------------------------------------------

def _requires_human_gate(agent_name: str, project_root: Path) -> bool:
    """Return True if the agent's YAML has requires_human_gate_after: true."""
    try:
        spec = load_agent(agent_name, resolve_agents_dir(project_root))
        return spec.requires_human_gate_after
    except Exception:
        return False


def _get_max_attempts(agent_name: str, project_root: Path) -> int:
    """Return max_attempts from agent YAML, defaulting to 3."""
    try:
        spec = load_agent(agent_name, resolve_agents_dir(project_root))
        return spec.max_attempts
    except Exception:
        return 3


# ---------------------------------------------------------------------------
# Task assembly helpers
# ---------------------------------------------------------------------------

def _format_handoff_section(h: HandoffNote) -> str:
    lines = [f"## Handoff From {h.from_agent}"]
    if h.decisions:
        lines.append("\n**Decisions Made:**")
        lines += [f"- {d}" for d in h.decisions]
    if h.requirements:
        lines.append("\n**Requirements:**")
        lines += [f"- {r}" for r in h.requirements]
    if h.artifacts:
        lines.append("\n**Artifacts:**")
        lines += [f"- {a}" for a in h.artifacts]
    if h.blockers:
        lines.append("\n**Blockers:**")
        lines += [f"- {b}" for b in h.blockers]
    if h.notes:
        lines.append(f"\n**Notes:** {h.notes}")
    return "\n".join(lines)


def _format_issues_section(accumulated_issues: list[dict]) -> str:
    lines = ["## Issues Found By Previous Agents", ""]
    for entry in accumulated_issues:
        lines.append(f"### {entry['agent']}")
        for issue in entry["issues"]:
            lines.append(f"- {issue}")
        lines.append("")
    return "\n".join(lines)


def _build_task_for_agent(
    base_task: str,
    accumulated_issues: list[dict],
    last_handoff: HandoffNote | None,
) -> str:
    """Assemble the task string for the next agent.

    Layers (in order):
      1. base_task — original user task (may include ProjectMemory section)
      2. HandoffNote section — structured decisions/requirements from the previous agent
      3. Issues section — issues accumulated across all previous agents
    """
    parts = [base_task]
    if last_handoff:
        parts.append(_format_handoff_section(last_handoff))
    if accumulated_issues:
        parts.append(_format_issues_section(accumulated_issues))
    return "\n\n---\n\n".join(parts)


def _save_handoff_file(run_id: str, handoff: HandoffNote, runs_dir: Path) -> None:
    path = runs_dir / run_id / "handoff.json"
    path.write_text(json.dumps(handoff.to_dict(), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Core execution loop (shared by start and resume)
# ---------------------------------------------------------------------------

def _execute(
    state: PipelineState,
    project_root: Path,
    hook_system: HookSystem,
    converse: bool = True,
) -> PipelineState:
    """
    Run agents in sequence starting from state.current_agent.

    converse=True (default):
      When an agent returns NEEDS_USER_INPUT, enter an interactive
      conversation loop instead of just pausing. The loop runs until
      the agent returns SUCCESS/FAILED/BLOCKED, then the pipeline continues.

    converse=False:
      Legacy behaviour — save state and return (requires manual --resume).

    Stops (and saves state) on:
      - BLOCKED  → status = "blocked"
      - FAILED   → status = "failed"
      - NEEDS_USER_INPUT + converse=False → status = "paused"
      - requires_human_gate_after = true → status = "paused"
      - No next agent → status = "complete"
    """
    runs_dir = project_root / "runs"
    current = state.current_agent
    step = len(state.completed)

    run_ctx = RunContext.load_or_create(state.run_id, runs_dir)

    while current:
        step += 1

        # ── max_attempts check (failure retries only) ────────────────────────
        fail_attempts = state.attempt_counts.get(current, 0)
        max_attempts = _get_max_attempts(current, project_root)
        if fail_attempts >= max_attempts:
            log.warning(
                "Agent %s has reached max_attempts=%d — escalating",
                current, max_attempts,
            )
            report_path = write_escalation_report(
                run_id=state.run_id,
                failed_agent=current,
                failure_chain=state.completed,
                last_error=state.failed_reason or f"max_attempts={max_attempts} exceeded",
                runs_dir=runs_dir,
            )
            state.status = "escalated"
            state.current_agent = current
            state.failed_agent = current
            state.failed_reason = f"max_attempts={max_attempts} exceeded for {current}"
            state.escalation_report_path = str(report_path)
            _save(state, runs_dir)
            hook_system.emit(ON_MAX_ATTEMPTS_REACHED, {
                "run_id": state.run_id,
                "agent": current,
                "attempts": fail_attempts,
                "max_attempts": max_attempts,
                "escalation_report": str(report_path),
            })
            hook_system.emit(ON_ESCALATION, {
                "run_id": state.run_id,
                "agent": current,
                "reason": state.failed_reason,
                "escalation_report": str(report_path),
                "completed_steps": len(state.completed),
            })
            return state

        feature_n = state.feature_counts.get(current, 0) + 1
        log.info(
            "[step %d] %s (feature=%d try=%d/%d)",
            step, current, feature_n, fail_attempts + 1, max_attempts,
        )

        last_handoff = HandoffNote.from_dict(state.last_handoff) if state.last_handoff else None
        task_for_agent = _build_task_for_agent(state.task, state.accumulated_issues, last_handoff)
        step_started_at = _now_iso()
        status_block, _ = run_agent(
            agent_name=current,
            task=task_for_agent,
            provider_name=state.provider,
            project_root=project_root,
            run_id=state.run_id,
            hook_system=hook_system,
            run_context=run_ctx,
        )

        # Capture HandoffNote from this agent for the next agent
        if status_block.handoff:
            state.last_handoff = status_block.handoff.to_dict()
            _save_handoff_file(state.run_id, status_block.handoff, runs_dir)

        # Update shared RunContext with this agent's outputs
        run_ctx.update_from_status_block(status_block)
        run_ctx.save(runs_dir)

        # Collect issues from this agent for downstream agents
        if status_block.issues_found:
            state.accumulated_issues.append({
                "agent": current,
                "issues": status_block.issues_found,
            })

        state.completed.append({
            "step": step,
            "agent": current,
            "task_id": status_block.task_id,
            "status": status_block.status.value,
            "feature": feature_n,
            "fail_attempt": fail_attempts + 1,
            "started_at": step_started_at,
            "completed_at": _now_iso(),
        })
        # Save after each step — durable even on crash
        state.status = "running"
        _save(state, runs_dir)

        # ── BLOCKED ──────────────────────────────────────────────────────────
        if status_block.status == StatusCode.BLOCKED:
            # Some agents (code_reviewer, qa_engineer) can hand off to debugger_engineer
            debug_agent = debugger_target(current)
            if debug_agent:
                state.debugger_from = current
                log.info(
                    "Agent %s BLOCKED — routing to %s for debugging (debugger_from=%s)",
                    current, debug_agent, current,
                )
                current = debug_agent
                continue
            state.status = "blocked"
            state.current_agent = current
            state.failed_agent = current
            state.failed_reason = status_block.summary
            _save(state, runs_dir)
            return state

        # ── Resolve next agent ────────────────────────────────────────────────
        # State machine is always authoritative for routing.
        # LLM's next_agent is logged as a recommendation but never drives routing.
        recommended_next = status_block.next_agent
        nxt: str = state_next_agent(
            current,
            status_block.status.value,
            recovery_from=state.recovery_from,
            debugger_from=state.debugger_from,
        ) or ""
        if recommended_next and recommended_next != nxt:
            log.info(
                "LLM recommended next_agent=%r but state machine routes to %r",
                recommended_next, nxt,
            )

        # ── FAILED ───────────────────────────────────────────────────────────
        if status_block.status == StatusCode.FAILED:
            # qa_engineer and code_reviewer: route to debugger_engineer so it
            # can fix the issues and hand back for re-testing.
            debug_agent = debugger_target(current)
            if debug_agent:
                state.attempt_counts[current] = fail_attempts + 1
                state.debugger_from = current
                log.info(
                    "Agent %s FAILED — routing to %s (attempt %d/%d)",
                    current, debug_agent,
                    fail_attempts + 1, _get_max_attempts(current, project_root),
                )
                current = debug_agent
                continue
            # All other agents: stop the pipeline.
            state.attempt_counts[current] = fail_attempts + 1
            state.status = "failed"
            state.current_agent = current
            state.failed_agent = current
            state.failed_reason = status_block.summary
            _save(state, runs_dir)
            return state

        # ── FEATURE_COMPLETE → increment feature counter, reset failure retries ─
        if status_block.status == StatusCode.FEATURE_COMPLETE:
            state.feature_counts[current] = feature_n
            state.attempt_counts[current] = 0
            log.info("Agent %s completed feature %d — looping back", current, feature_n)
            current = nxt
            continue

        # ── NEEDS_USER_INPUT ─────────────────────────────────────────────────
        if status_block.status == StatusCode.NEEDS_USER_INPUT:
            if converse:
                # Enter interactive conversation loop — same agent continues
                log.info(
                    "Agent %s needs user input — entering conversation loop",
                    current,
                )
                status_block, _ = enter_conversation_loop(
                    agent_name=current,
                    task=task_for_agent,
                    provider_name=state.provider,
                    project_root=project_root,
                    run_id=state.run_id,
                    initial_summary=status_block.next_recommended_action or status_block.summary,
                    hook_system=hook_system,
                )
                # Re-resolve nxt based on the new status
                nxt = state_next_agent(
                    current,
                    status_block.status.value,
                    recovery_from=state.recovery_from,
                ) or ""
                # Collect any new issues from the conversation
                if status_block.issues_found:
                    state.accumulated_issues.append({
                        "agent": current,
                        "issues": status_block.issues_found,
                    })
                # Don't return — fall through to normal routing below
            else:
                # Legacy: pause and save (requires manual harness pipeline --resume)
                state.status = "paused"
                state.pause_substate = "needs_user_input"
                state.current_agent = nxt
                state.pause_reason = (
                    f"Agent '{current}' needs user input: "
                    f"{status_block.next_recommended_action}"
                )
                _save(state, runs_dir)
                hook_system.emit(ON_HUMAN_APPROVAL_REQUIRED, {
                    "run_id": state.run_id,
                    "agent": current,
                    "reason": state.pause_reason,
                    "next_agent": nxt,
                    "pause_substate": state.pause_substate,
                })
                hook_system.emit(ON_WORKFLOW_PAUSED, {
                    "run_id": state.run_id,
                    "pause_substate": state.pause_substate,
                    "agent": current,
                    "next_agent": nxt,
                    "reason": state.pause_reason,
                })
                return state

        # ── SUCCESS → reset failure retry counter ────────────────────────────
        if status_block.status == StatusCode.SUCCESS:
            state.attempt_counts[current] = 0

        # ── SUCCESS + human gate ──────────────────────────────────────────────
        if (
            status_block.status == StatusCode.SUCCESS
            and nxt
            and _requires_human_gate(current, project_root)
        ):
            state.status = "paused"
            state.pause_substate = "waiting_for_human"
            state.current_agent = nxt
            state.pause_reason = (
                f"Human gate after '{current}'. "
                f"Review the output above, make any manual changes, then resume."
            )
            _save(state, runs_dir)
            hook_system.emit(ON_HUMAN_APPROVAL_REQUIRED, {
                "run_id": state.run_id,
                "agent": current,
                "reason": state.pause_reason,
                "next_agent": nxt,
                "pause_substate": state.pause_substate,
            })
            hook_system.emit(ON_WORKFLOW_PAUSED, {
                "run_id": state.run_id,
                "pause_substate": state.pause_substate,
                "agent": current,
                "next_agent": nxt,
                "reason": state.pause_reason,
            })
            return state

        current = nxt

    # ── No next agent — pipeline complete ─────────────────────────────────────
    state.status = "complete"
    state.current_agent = ""
    _save(state, runs_dir)
    hook_system.emit(ON_WORKFLOW_COMPLETE, {
        "run_id": state.run_id,
        "start_agent": state.start_agent,
        "completed_steps": len(state.completed),
        "completed": state.completed,
    })

    # Extract and persist key facts from this run for future pipelines
    try:
        entry = ProjectMemory.extract_from_run(
            run_id=state.run_id,
            task=state.task,
            completed=state.completed,
            run_context=run_ctx,
            provider_name=state.provider,
            project_root=project_root,
        )
        if entry:
            ProjectMemory.append(entry, project_root)
    except Exception:
        log.warning("ProjectMemory extraction failed — skipping (non-fatal)")

    return state


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_pipeline(
    start_agent: str,
    task: str,
    provider: str,
    project_root: Path,
    hook_system: HookSystem | None = None,
    converse: bool = True,
) -> PipelineState:
    """Start a new pipeline from start_agent."""
    from harness.integrations.slack import load_slack_config, SlackNotifier  # noqa: PLC0415

    hs = hook_system or default_hooks
    runs_dir = project_root / "runs"

    # Inject past-run memories into the task so all agents benefit from
    # cross-session knowledge (stack choices, patterns, constraints).
    memories = ProjectMemory.load(project_root)
    if memories:
        memory_section = ProjectMemory.to_prompt_section(memories)
        task = task + "\n\n---\n\n" + memory_section

    run_id = generate_run_id()
    state = PipelineState(
        run_id=run_id,
        task=task,
        start_agent=start_agent,
        provider=provider,
        status="running",
        current_agent=start_agent,
        created_at=_now_iso(),
    )
    _save(state, runs_dir)

    # Wire Slack: send parent message → store thread_ts → register hooks
    slack_cfg = load_slack_config(project_root)
    if slack_cfg and slack_cfg.can_send:
        notifier = SlackNotifier(slack_cfg)
        ts = notifier.send_pipeline_started(run_id, start_agent, task, runs_dir)
        if ts:
            state.slack_thread_ts = ts
            _save(state, runs_dir)
        notifier.register_hooks(hs, state, runs_dir)

    return _execute(state, project_root, hs, converse=converse)


def resume_pipeline(
    run_id: str,
    project_root: Path,
    provider: str | None = None,
    user_input: str | None = None,
    hook_system: HookSystem | None = None,
    converse: bool = True,
) -> PipelineState:
    """Resume a paused pipeline.

    user_input:
        When the pipeline paused with pause_substate="needs_user_input",
        pass the user's response here. It is appended to the task context
        so the next agent sees it.
    """
    from harness.integrations.slack import load_slack_config, SlackNotifier  # noqa: PLC0415

    hs = hook_system or default_hooks
    runs_dir = project_root / "runs"

    state = load_state(run_id, runs_dir)

    if state.status != "paused":
        raise ValueError(
            f"Pipeline {run_id!r} cannot be resumed — current status is {state.status!r}.\n"
            "Only 'paused' pipelines can be resumed."
        )
    if not state.current_agent:
        raise ValueError(
            f"Pipeline {run_id!r} has no current_agent to resume from."
        )

    if provider:
        state.provider = provider

    # Inject user input into task context when agent asked for it
    if user_input and state.pause_substate == "needs_user_input":
        requester = state.failed_agent or state.current_agent
        state.task = (
            state.task
            + f"\n\n---\n\n## User Input (in response to {requester})\n\n{user_input}"
        )
        state.user_inputs.append({
            "at": _now_iso(),
            "requested_by": requester,
            "input": user_input,
        })
        _save(state, runs_dir)
        log.info("User input injected for agent=%s length=%d", requester, len(user_input))

    # Re-wire Slack hooks (with existing thread_ts from state) before emitting resumed event
    slack_cfg = load_slack_config(project_root)
    if slack_cfg and slack_cfg.can_send:
        notifier = SlackNotifier(slack_cfg)
        notifier.register_hooks(hs, state, runs_dir)

    hs.emit(ON_WORKFLOW_RESUMED, {
        "run_id": run_id,
        "current_agent": state.current_agent,
        "completed_steps": len(state.completed),
        "previous_pause_substate": state.pause_substate,
    })

    state.status = "running"
    state.pause_substate = ""
    return _execute(state, project_root, hs, converse=converse)
