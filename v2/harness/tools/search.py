from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from harness.tools.file_ops import ToolError

log = logging.getLogger(__name__)

# Directories to skip during search (same as codebase_map walk)
_SKIP_DIRS_FOR_SEARCH = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    ".tox", ".venv", "venv", ".env", "env",
    "node_modules", ".next", "dist", "build",
})

_MAX_RESULTS = 200
_MAX_LINE_LEN = 300   # truncate very long lines in results


def search_code(
    pattern: str,
    project_root: Path,
    path: str = ".",
    file_pattern: str | None = None,
    case_sensitive: bool = False,
    max_results: int = _MAX_RESULTS,
) -> dict:
    """
    Search for a regex pattern in text files under path.

    Args:
        pattern:        Python regex pattern to search for.
        project_root:   Absolute project root.
        path:           Directory to search within (relative to project_root).
        file_pattern:   Optional glob to filter files (e.g. "*.py").
        case_sensitive: Default False (case-insensitive search).
        max_results:    Cap on number of matching lines returned.

    Returns:
        {
            "pattern": str,
            "path": str,
            "matches": [
                {"file": str, "line_number": int, "line": str},
                ...
            ],
            "total_matches": int,
            "truncated": bool,
            "files_searched": int,
        }
    """
    from harness.tools.file_ops import _safe_resolve

    search_root = _safe_resolve(path, project_root)
    if not search_root.exists():
        raise ToolError(f"Search path does not exist: '{path}'")
    if not search_root.is_dir():
        raise ToolError(f"Search path is not a directory: '{path}'")

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ToolError(f"Invalid regex pattern: {exc}") from exc

    root_resolved = project_root.resolve()
    matches: list[dict] = []
    truncated = False
    files_searched = 0

    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in _SKIP_DIRS_FOR_SEARCH]

        for fname in sorted(filenames):
            if file_pattern and not Path(fname).match(file_pattern):
                continue

            full_path = Path(dirpath) / fname
            try:
                rel = full_path.resolve().relative_to(root_resolved).as_posix()
            except ValueError:
                continue

            # Skip obvious binaries by suffix
            suffix = full_path.suffix.lower()
            if suffix in {
                ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
                ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
                ".pdf", ".zip", ".tar", ".gz", ".whl",
            }:
                continue

            try:
                text = full_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            files_searched += 1

            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    if len(matches) >= max_results:
                        truncated = True
                        break
                    display_line = line[:_MAX_LINE_LEN]
                    if len(line) > _MAX_LINE_LEN:
                        display_line += " ..."
                    matches.append({
                        "file": rel,
                        "line_number": lineno,
                        "line": display_line,
                    })

            if truncated:
                break
        if truncated:
            break

    log.debug(
        "search_code pattern=%r files_searched=%d matches=%d truncated=%s",
        pattern, files_searched, len(matches), truncated,
    )
    return {
        "pattern": pattern,
        "path": path,
        "matches": matches,
        "total_matches": len(matches),
        "truncated": truncated,
        "files_searched": files_searched,
    }
