from __future__ import annotations

import logging
from pathlib import Path

from harness.report import is_protected_path

log = logging.getLogger(__name__)

# Hard limit on single file read to avoid flooding the LLM context.
_MAX_READ_LINES = 2000
_MAX_READ_BYTES = 512_000  # 512 KB


class ToolError(Exception):
    """Raised by tool handlers when the request is invalid or forbidden."""


def _safe_resolve(rel_or_abs: str, project_root: Path) -> Path:
    """
    Resolve a path string relative to project_root.

    Raises ToolError on path traversal (would escape project_root).
    """
    target = (project_root / rel_or_abs).resolve()
    try:
        target.relative_to(project_root.resolve())
    except ValueError:
        raise ToolError(
            f"Path traversal denied: '{rel_or_abs}' resolves outside project root."
        )
    return target


# ---------------------------------------------------------------------------
# LIST_FILES
# ---------------------------------------------------------------------------

def list_files(
    path: str,
    project_root: Path,
    recursive: bool = False,
) -> dict:
    """
    List files/directories under path (relative to project_root).

    Returns:
        {
            "path": str,
            "entries": [{"name": str, "type": "file"|"dir", "size_bytes": int}, ...],
            "truncated": bool,
        }

    Hard limit: 500 entries returned (sorted).
    """
    MAX_ENTRIES = 500

    target = _safe_resolve(path, project_root)

    if not target.exists():
        raise ToolError(f"Path does not exist: '{path}'")
    if not target.is_dir():
        raise ToolError(f"Path is not a directory: '{path}'")

    entries: list[dict] = []
    truncated = False

    root_resolved = project_root.resolve()

    if recursive:
        walker = sorted(target.rglob("*"))
    else:
        walker = sorted(target.iterdir())

    for p in walker:
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        try:
            size = p.stat().st_size if p.is_file() else 0
        except OSError:
            size = 0
        rel = p.resolve().relative_to(root_resolved).as_posix()
        entries.append({
            "name": rel,
            "type": "file" if p.is_file() else "dir",
            "size_bytes": size,
        })

    log.debug("list_files path=%s entries=%d truncated=%s", path, len(entries), truncated)
    return {
        "path": path,
        "entries": entries,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# READ_FILE
# ---------------------------------------------------------------------------

def read_file(
    path: str,
    project_root: Path,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict:
    """
    Read a file and return its contents.

    - Path traversal is rejected.
    - specs/prd.md is readable (agents may read, not write).
    - Returns up to _MAX_READ_LINES lines or _MAX_READ_BYTES.
    - start_line / end_line are 1-indexed, inclusive.

    Returns:
        {
            "path": str,
            "content": str,
            "total_lines": int,
            "returned_lines": int,
            "truncated": bool,
            "encoding": "utf-8",
        }
    """
    target = _safe_resolve(path, project_root)

    if not target.exists():
        raise ToolError(f"File not found: '{path}'")
    if not target.is_file():
        raise ToolError(f"Path is not a file: '{path}'")

    # Check size first to avoid reading huge binaries
    size = target.stat().st_size
    if size > _MAX_READ_BYTES and start_line is None and end_line is None:
        raise ToolError(
            f"File too large ({size:,} bytes). "
            f"Use start_line/end_line to read a specific range "
            f"(max {_MAX_READ_LINES} lines per call)."
        )

    try:
        raw_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        raise ToolError(f"Could not read file: {exc}") from exc

    total_lines = len(raw_lines)

    # Apply line range (1-indexed, inclusive)
    lo = max(1, start_line or 1)
    hi = min(total_lines, end_line or total_lines)
    selected = raw_lines[lo - 1 : hi]

    truncated = False
    if len(selected) > _MAX_READ_LINES:
        selected = selected[:_MAX_READ_LINES]
        truncated = True

    content = "\n".join(selected)
    returned_lines = len(selected)

    log.debug(
        "read_file path=%s lines=%d-%d/%d truncated=%s",
        path, lo, lo + returned_lines - 1, total_lines, truncated,
    )
    return {
        "path": path,
        "content": content,
        "total_lines": total_lines,
        "returned_lines": returned_lines,
        "start_line": lo,
        "end_line": lo + returned_lines - 1,
        "truncated": truncated,
        "size_bytes": size,
        "encoding": "utf-8",
    }
