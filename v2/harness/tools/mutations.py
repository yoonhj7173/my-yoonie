from __future__ import annotations

"""
Filesystem mutation tools: PATCH_FILE and WRITE_FILE.

Design principles (per harness spec):
  - Inspectable:  diff preview generated before every write.
  - Reversible:   backup created before every PATCH_FILE.
  - Auditable:    every mutation logged to mutations.jsonl.
  - Safe:         protected paths blocked; WRITE_FILE cannot overwrite.
  - Observable:   ON_HUMAN_APPROVAL_REQUIRED hook emitted for large diffs.

EXEC_COMMAND is NOT implemented here. Shell execution requires separate
safety gates (allowlist, dry-run preview, pty isolation) that are out of
scope for this phase.
"""

import difflib
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from harness.hooks import HookSystem, ON_HUMAN_APPROVAL_REQUIRED
from harness.report import is_protected_path
from harness.tools.audit import log_mutation
from harness.tools.file_ops import ToolError, _safe_resolve

log = logging.getLogger(__name__)

# Diffs larger than this trigger ON_HUMAN_APPROVAL_REQUIRED.
_LARGE_DIFF_THRESHOLD = 50  # lines


# ---------------------------------------------------------------------------
# MutationContext
# ---------------------------------------------------------------------------

@dataclass
class MutationContext:
    """
    Per-agent-run context passed to all mutation tools.

    Carries the identifiers needed for audit logging and hook emission
    without threading them through every function signature individually.
    """
    run_id: str
    agent_name: str
    task_id: str
    runs_dir: Path
    project_root: Path
    hook_system: HookSystem | None = None
    # PIDs of background processes started with EXEC_COMMAND background=true.
    # Runner kills these automatically after the agent's tool loop ends.
    background_pids: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.background_pids is None:
            self.background_pids = []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_backup(target: Path, ctx: MutationContext) -> str:
    """
    Copy target to runs/<run_id>/backups/ before mutation.
    Returns the absolute path of the backup file.
    """
    backup_dir = ctx.runs_dir / ctx.run_id / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Flatten the path to a filename, keeping the last 60 chars to avoid
    # overly long filenames on macOS (255-byte limit).
    safe_name = target.as_posix().replace("/", "_")[-60:]
    backup_path = backup_dir / f"{ctx.agent_name}-{ts}-{safe_name}"
    shutil.copy2(target, backup_path)
    log.debug("Backup created: %s → %s", target, backup_path)
    return str(backup_path)


def _unified_diff(old: str, new: str, path: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    return "\n".join(lines)


def _maybe_emit_approval(
    ctx: MutationContext,
    tool: str,
    path: str,
    reason: str,
    diff_preview: str,
) -> None:
    """
    Emit ON_HUMAN_APPROVAL_REQUIRED.
    Does not block the operation — prepares the runtime flow for future
    interactive approval (Slack, mobile) without requiring it now.
    """
    if not ctx.hook_system:
        return
    ctx.hook_system.emit(ON_HUMAN_APPROVAL_REQUIRED, {
        "run_id": ctx.run_id,
        "agent": ctx.agent_name,
        "tool": tool,
        "path": path,
        "reason": reason,
        "diff_preview": diff_preview[:500],
    })


# ---------------------------------------------------------------------------
# PATCH_FILE
# ---------------------------------------------------------------------------

def patch_file(request: dict, ctx: MutationContext) -> dict:
    """
    Apply a search/replace patch to an existing file.

    Request fields:
      path    — project-relative path to the file (required)
      search  — exact string to replace; must appear exactly once (required)
      replace — replacement string (required; empty string = deletion)
      reason  — human-readable rationale (logged only, not required)

    Safeguards:
      - Protected paths (specs/prd.md) are blocked outright.
      - Search string must appear exactly once (no ambiguous multi-match).
      - Backup created before any write.
      - Large diffs (>{threshold} lines) trigger ON_HUMAN_APPROVAL_REQUIRED.
      - Every outcome (success or failure) is logged to mutations.jsonl.
    """
    path_str = request.get("path", "")
    search = request.get("search")
    replace = request.get("replace")

    if not path_str:
        raise ToolError("PATCH_FILE: 'path' is required.")
    if search is None:
        raise ToolError("PATCH_FILE: 'search' is required.")
    if replace is None:
        raise ToolError("PATCH_FILE: 'replace' is required.")

    target = _safe_resolve(path_str, ctx.project_root)

    # ── Safety gates ──────────────────────────────────────────────────────────
    if is_protected_path(target, ctx.project_root):
        raise ToolError(
            f"PATCH_FILE: '{path_str}' is a protected spec file "
            "and cannot be modified by agents. "
            "(Protected: specs/prd.md, specs/tech-design.md)"
        )
    if not target.exists():
        raise ToolError(
            f"PATCH_FILE: file not found: '{path_str}'. "
            "Use WRITE_FILE to create a new file."
        )
    if not target.is_file():
        raise ToolError(f"PATCH_FILE: '{path_str}' is not a regular file.")

    old_content = target.read_text(encoding="utf-8")

    count = old_content.count(search)
    if count == 0:
        _audit_fail(ctx, "PATCH_FILE", path_str, "search_replace", "Search string not found.")
        raise ToolError(
            f"PATCH_FILE: search string not found in '{path_str}'. "
            "Verify the string matches exactly (including whitespace and indentation)."
        )
    if count > 1:
        _audit_fail(ctx, "PATCH_FILE", path_str, "search_replace",
                    f"Search string found {count} times (must be unique).")
        raise ToolError(
            f"PATCH_FILE: search string found {count} times in '{path_str}'. "
            "Provide a longer, more specific search string that uniquely identifies the target."
        )

    new_content = old_content.replace(search, replace, 1)
    diff_text = _unified_diff(old_content, new_content, path_str)
    diff_lines = diff_text.count("\n")

    # ── Backup ────────────────────────────────────────────────────────────────
    backup_path = _make_backup(target, ctx)

    # ── Approval hook for large diffs ─────────────────────────────────────────
    if diff_lines > _LARGE_DIFF_THRESHOLD:
        _maybe_emit_approval(
            ctx, "PATCH_FILE", path_str,
            f"Large diff: {diff_lines} lines changed in '{path_str}'. Review before applying.",
            diff_text,
        )
        log.info("PATCH_FILE: large diff (%d lines) for '%s'", diff_lines, path_str)

    # ── Write ────────────────────────────────────────────────────────────────
    try:
        target.write_text(new_content, encoding="utf-8")
    except Exception as exc:
        log_mutation(
            runs_dir=ctx.runs_dir, run_id=ctx.run_id,
            agent_name=ctx.agent_name, task_id=ctx.task_id,
            tool="PATCH_FILE", path=path_str,
            mutation_type="search_replace",
            diff_preview=diff_text, backup_path=backup_path,
            success=False, error=str(exc),
        )
        raise ToolError(f"PATCH_FILE: write failed: {exc}") from exc

    log_mutation(
        runs_dir=ctx.runs_dir, run_id=ctx.run_id,
        agent_name=ctx.agent_name, task_id=ctx.task_id,
        tool="PATCH_FILE", path=path_str,
        mutation_type="search_replace",
        diff_preview=diff_text, backup_path=backup_path,
        success=True,
    )

    return {
        "tool": "PATCH_FILE",
        "status": "ok",
        "path": path_str,
        "diff_preview": diff_text[:2000] + ("\n…(truncated)" if len(diff_text) > 2000 else ""),
        "diff_lines": diff_lines,
        "backup_path": backup_path,
        "lines_before": len(old_content.splitlines()),
        "lines_after": len(new_content.splitlines()),
    }


# ---------------------------------------------------------------------------
# WRITE_FILE
# ---------------------------------------------------------------------------

def write_file(request: dict, ctx: MutationContext) -> dict:
    """
    Create a new file. Blocked if the file already exists.

    WRITE_FILE is intentionally restricted to new file creation.
    To modify an existing file, use PATCH_FILE.

    Request fields:
      path    — project-relative path (required)
      content — full file content as a string (required)
      reason  — rationale (logged only)
    """
    path_str = request.get("path", "")
    content = request.get("content")

    if not path_str:
        raise ToolError("WRITE_FILE: 'path' is required.")
    if content is None:
        raise ToolError("WRITE_FILE: 'content' is required.")
    if not isinstance(content, str):
        raise ToolError("WRITE_FILE: 'content' must be a string.")

    target = _safe_resolve(path_str, ctx.project_root)

    # specs/prd.md is always blocked for agents (human-only).
    # specs/tech-design.md: block WRITE_FILE if it already exists (use PATCH_FILE — which is
    # also blocked, so effectively immutable once created). First-time creation is allowed
    # for system_architect via WRITE_FILE on a new file.
    if is_protected_path(target, ctx.project_root) and target.exists():
        raise ToolError(
            f"WRITE_FILE: '{path_str}' is a protected spec file that already exists "
            "and cannot be overwritten by agents. "
            "(Protected: specs/prd.md, specs/tech-design.md)"
        )
    # prd.md is always blocked regardless
    from harness.report import _PROTECTED_SPECS  # noqa: PLC0415
    prd = (ctx.project_root / "specs" / "prd.md").resolve()
    if target.resolve() == prd:
        raise ToolError(
            "WRITE_FILE: 'specs/prd.md' is a human-only file and cannot be written by agents."
        )
    if target.exists():
        raise ToolError(
            f"WRITE_FILE: '{path_str}' already exists. "
            "Use PATCH_FILE to modify existing files. "
            "WRITE_FILE is restricted to creating new files only."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(content, encoding="utf-8")
    except Exception as exc:
        log_mutation(
            runs_dir=ctx.runs_dir, run_id=ctx.run_id,
            agent_name=ctx.agent_name, task_id=ctx.task_id,
            tool="WRITE_FILE", path=path_str,
            mutation_type="new_file",
            diff_preview="", backup_path=None,
            success=False, error=str(exc),
        )
        raise ToolError(f"WRITE_FILE: write failed: {exc}") from exc

    lines_written = len(content.splitlines())
    size_bytes = len(content.encode("utf-8"))

    log_mutation(
        runs_dir=ctx.runs_dir, run_id=ctx.run_id,
        agent_name=ctx.agent_name, task_id=ctx.task_id,
        tool="WRITE_FILE", path=path_str,
        mutation_type="new_file",
        diff_preview=f"(new file: {lines_written} lines, {size_bytes} bytes)",
        backup_path=None,
        success=True,
    )

    # Emit approval hook — wired for future interactive approval flows.
    _maybe_emit_approval(
        ctx, "WRITE_FILE", path_str,
        f"New file created by agent '{ctx.agent_name}': '{path_str}'",
        "",
    )

    return {
        "tool": "WRITE_FILE",
        "status": "ok",
        "path": path_str,
        "lines_written": lines_written,
        "size_bytes": size_bytes,
    }


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _audit_fail(
    ctx: MutationContext,
    tool: str,
    path: str,
    mutation_type: str,
    error: str,
) -> None:
    log_mutation(
        runs_dir=ctx.runs_dir, run_id=ctx.run_id,
        agent_name=ctx.agent_name, task_id=ctx.task_id,
        tool=tool, path=path,
        mutation_type=mutation_type,
        diff_preview="", backup_path=None,
        success=False, error=error,
    )
