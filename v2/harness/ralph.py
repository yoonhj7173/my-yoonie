from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from harness.context import RunContext
from harness.hooks import HookSystem, hooks as default_hooks
from harness.pipeline import (
    PipelineState,
    _build_task_for_agent,
    _save,
    _save_handoff_file,
    _now_iso,
)
from harness.runner import run_agent
from harness.status import HandoffNote, IssueItem, StatusCode, generate_run_id

log = logging.getLogger(__name__)

_DEFAULT_MAX_ITERATIONS = 5   # max SE→QA cycles for the initial pass
_DEFAULT_MAX_PER_ISSUE = 3    # max SE fix attempts per individual issue


# ---------------------------------------------------------------------------
# Task builders
# ---------------------------------------------------------------------------

def _build_issue_fix_task(base_task: str, issue: IssueItem) -> str:
    """Build the SE task for fixing one specific issue, with full attempt history."""
    lines = [
        base_task,
        "---",
        f"## Fix Required: {issue.id}",
        "",
        f"**Description:** {issue.description}",
        f"**Severity:** {issue.severity}",
        f"**Found by:** {issue.found_by}",
    ]

    if issue.attempts:
        lines += ["", f"## Previous Fix Attempts ({len(issue.attempts)} failed)", ""]
        for a in issue.attempts:
            lines.append(f"### Attempt {a.get('attempt_num', '?')}")
            if a.get("approach"):
                lines.append(f"- **What you tried:** {a['approach']}")
            if a.get("result"):
                lines.append(f"- **Why it failed:** {a['result']}")
            lines.append("")

    lines += [
        "## Instructions",
        "Fix ONLY this specific issue. Do not refactor unrelated code.",
        "In your HandoffNote, set `attempts[0].approach` to describe exactly what you changed and why.",
    ]
    return "\n".join(lines)


def _build_issue_verify_task(base_task: str, issue: IssueItem) -> str:
    """Build the QA task for verifying one specific issue was fixed."""
    last_attempt = issue.attempts[-1] if issue.attempts else {}
    lines = [
        base_task,
        "---",
        f"## Verify Fix: {issue.id}",
        "",
        f"**Description:** {issue.description}",
        f"**Severity:** {issue.severity}",
    ]

    if last_attempt.get("approach"):
        lines += [
            "",
            "## What SE Changed This Attempt",
            last_attempt["approach"],
        ]

    lines += [
        "",
        "## Instructions",
        "Test ONLY this specific issue.",
        "- If it is fixed: return `SUCCESS`",
        "- If still failing: return `FAILED` and describe exactly what still fails in `summary`.",
        "Do NOT test other issues right now.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _issues_from_block(block, agent_name: str) -> list[IssueItem]:
    """Extract structured issues from a status block.

    Prefers block.issues_list (structured). Falls back to block.issues_found
    (flat strings) for backward compat with agents that haven't been updated yet.
    """
    if block.issues_list:
        return block.issues_list

    # Fallback: convert flat strings → IssueItem
    items = []
    for i, desc in enumerate(block.issues_found):
        items.append(IssueItem(
            id=f"issue_{i + 1}",
            description=desc,
            severity="major",
            found_by=agent_name,
        ))
    return items


def _record_attempt(
    issue: IssueItem,
    attempt_num: int,
    se_block,
    qa_summary: str = "",
) -> None:
    """Append a fix attempt record to an issue."""
    approach = ""
    if se_block.handoff and se_block.handoff.attempts:
        approach = se_block.handoff.attempts[0].get("approach", "")
    elif se_block.handoff and se_block.handoff.notes:
        approach = se_block.handoff.notes
    elif se_block.summary:
        approach = se_block.summary

    issue.attempts.append({
        "attempt_num": attempt_num,
        "approach": approach,
        "result": qa_summary,
        "at": _now_iso(),
    })


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_ralph_loop(
    task: str,
    provider: str,
    project_root: Path,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    max_per_issue: int = _DEFAULT_MAX_PER_ISSUE,
    hook_system: HookSystem | None = None,
) -> PipelineState:
    """
    Two-phase SE↔QA loop.

    Phase 1 — Initial pass:
      SE implements (or attempts a fix for all issues) → QA runs.
      If QA passes: done.
      If QA finds issues: enter Phase 2.

    Phase 2 — Per-issue resolution:
      For each issue in order:
        while not verified and attempts < max_per_issue:
          SE.fix(issue + attempt_history) → QA.verify(issue)
          on pass: issue.status = "verified", next issue
          on fail: record attempt, retry SE

    Rationale for two phases:
      Phase 1 catches the common case (SE builds it right, QA approves).
      Phase 2 handles the targeted fix-verify loop without running SE on
      the full codebase for every individual issue check.
    """
    hs = hook_system or default_hooks
    runs_dir = project_root / "runs"
    run_id = generate_run_id()

    state = PipelineState(
        run_id=run_id,
        task=task,
        start_agent="software_engineer",
        provider=provider,
        status="running",
        current_agent="software_engineer",
        created_at=_now_iso(),
    )
    _save(state, runs_dir)
    run_ctx = RunContext.load_or_create(run_id, runs_dir)
    step = 0

    # ── Phase 1: Initial SE run ──────────────────────────────────────────────
    step += 1
    log.info("[ralph] phase 1 — initial SE run")
    state.current_agent = "software_engineer"
    se_block, _ = run_agent(
        "software_engineer", task, provider,
        project_root, run_id, hs, run_context=run_ctx,
    )
    run_ctx.update_from_status_block(se_block)
    run_ctx.save(runs_dir)

    if se_block.handoff:
        state.last_handoff = se_block.handoff.to_dict()
        _save_handoff_file(run_id, se_block.handoff, runs_dir)

    state.completed.append({
        "step": step, "agent": "software_engineer",
        "task_id": se_block.task_id, "status": se_block.status.value,
        "phase": 1, "started_at": _now_iso(), "completed_at": _now_iso(),
    })
    _save(state, runs_dir)

    if se_block.status in (StatusCode.FAILED, StatusCode.BLOCKED):
        state.status = se_block.status.value.lower()
        state.failed_agent = "software_engineer"
        state.failed_reason = se_block.summary
        _save(state, runs_dir)
        return state

    # ── Phase 1: Initial QA run ──────────────────────────────────────────────
    step += 1
    log.info("[ralph] phase 1 — initial QA run")
    state.current_agent = "qa_engineer"
    qa_task = _build_task_for_agent(task, [], se_block.handoff)
    qa_block, _ = run_agent(
        "qa_engineer", qa_task, provider,
        project_root, run_id, hs, run_context=run_ctx,
    )
    run_ctx.update_from_status_block(qa_block)
    run_ctx.save(runs_dir)

    state.completed.append({
        "step": step, "agent": "qa_engineer",
        "task_id": qa_block.task_id, "status": qa_block.status.value,
        "phase": 1, "started_at": _now_iso(), "completed_at": _now_iso(),
    })
    _save(state, runs_dir)

    if qa_block.status == StatusCode.SUCCESS:
        state.status = "complete"
        state.current_agent = ""
        _save(state, runs_dir)
        log.info("[ralph] QA passed on initial run — done")
        return state

    if qa_block.status == StatusCode.BLOCKED:
        state.status = "blocked"
        state.failed_agent = "qa_engineer"
        state.failed_reason = qa_block.summary
        _save(state, runs_dir)
        return state

    # ── Phase 2: Per-issue resolution loop ───────────────────────────────────
    issues = _issues_from_block(qa_block, "qa_engineer")
    if not issues:
        # QA failed but gave no structured issues — treat as escalated
        state.status = "escalated"
        state.failed_agent = "qa_engineer"
        state.failed_reason = qa_block.summary or "QA failed without issue details"
        _save(state, runs_dir)
        return state

    log.info("[ralph] phase 2 — %d issue(s) to resolve", len(issues))

    for issue in issues:
        issue.status = "in_progress"
        log.info("[ralph] resolving %s: %s", issue.id, issue.description[:60])

        for attempt_num in range(1, max_per_issue + 1):
            # SE fixes this specific issue
            step += 1
            state.current_agent = "software_engineer"
            se_fix_task = _build_issue_fix_task(task, issue)

            se_fix_block, _ = run_agent(
                "software_engineer", se_fix_task, provider,
                project_root, run_id, hs, run_context=run_ctx,
            )
            run_ctx.update_from_status_block(se_fix_block)
            run_ctx.save(runs_dir)

            if se_fix_block.handoff:
                state.last_handoff = se_fix_block.handoff.to_dict()
                _save_handoff_file(run_id, se_fix_block.handoff, runs_dir)

            state.completed.append({
                "step": step, "agent": "software_engineer",
                "task_id": se_fix_block.task_id, "status": se_fix_block.status.value,
                "phase": 2, "issue_id": issue.id, "attempt": attempt_num,
                "started_at": _now_iso(), "completed_at": _now_iso(),
            })
            _save(state, runs_dir)

            if se_fix_block.status in (StatusCode.FAILED, StatusCode.BLOCKED):
                state.status = se_fix_block.status.value.lower()
                state.failed_agent = "software_engineer"
                state.failed_reason = f"SE failed on {issue.id}: {se_fix_block.summary}"
                _save(state, runs_dir)
                return state

            # QA verifies this specific issue
            step += 1
            state.current_agent = "qa_engineer"
            _record_attempt(issue, attempt_num, se_fix_block)
            qa_verify_task = _build_issue_verify_task(task, issue)

            qa_verify_block, _ = run_agent(
                "qa_engineer", qa_verify_task, provider,
                project_root, run_id, hs, run_context=run_ctx,
            )
            run_ctx.update_from_status_block(qa_verify_block)
            run_ctx.save(runs_dir)

            state.completed.append({
                "step": step, "agent": "qa_engineer",
                "task_id": qa_verify_block.task_id, "status": qa_verify_block.status.value,
                "phase": 2, "issue_id": issue.id, "attempt": attempt_num,
                "started_at": _now_iso(), "completed_at": _now_iso(),
            })
            _save(state, runs_dir)

            if qa_verify_block.status == StatusCode.SUCCESS:
                # Update attempt record with verification result
                if issue.attempts:
                    issue.attempts[-1]["result"] = "verified ✓"
                issue.status = "verified"
                log.info("[ralph] %s verified on attempt %d", issue.id, attempt_num)
                break

            if qa_verify_block.status == StatusCode.BLOCKED:
                state.status = "blocked"
                state.failed_agent = "qa_engineer"
                state.failed_reason = qa_verify_block.summary
                _save(state, runs_dir)
                return state

            # Still failing — record why and retry SE
            if issue.attempts:
                issue.attempts[-1]["result"] = qa_verify_block.summary
            log.info(
                "[ralph] %s still failing after attempt %d/%d",
                issue.id, attempt_num, max_per_issue,
            )

        else:
            # max_per_issue exhausted for this issue
            issue.status = "failed"
            log.warning("[ralph] %s unresolved after %d attempts — escalating", issue.id, max_per_issue)
            state.status = "escalated"
            state.failed_agent = "software_engineer"
            state.failed_reason = (
                f"{issue.id} unresolved after {max_per_issue} SE attempts: {issue.description}"
            )
            _save(state, runs_dir)
            return state

    # All issues resolved
    all_verified = all(i.status == "verified" for i in issues)
    state.status = "complete" if all_verified else "escalated"
    state.current_agent = ""
    _save(state, runs_dir)
    log.info(
        "[ralph] done — %d/%d issues verified",
        sum(1 for i in issues if i.status == "verified"), len(issues),
    )
    return state
