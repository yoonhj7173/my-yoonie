from __future__ import annotations

"""
Mutation audit logger.

Every file mutation (PATCH_FILE, WRITE_FILE) appends a structured entry to
runs/<run_id>/mutations.jsonl. The file is newline-delimited JSON so it can
be streamed, grepped, and replayed without loading the entire run into memory.

Future recovery tooling and debugging will depend on this log.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_AUDIT_FILENAME = "mutations.jsonl"


def log_mutation(
    *,
    runs_dir: Path,
    run_id: str,
    agent_name: str,
    task_id: str,
    tool: str,
    path: str,
    mutation_type: str,
    diff_preview: str,
    backup_path: str | None,
    success: bool,
    error: str | None = None,
) -> None:
    """
    Append one mutation record to runs/<run_id>/mutations.jsonl.

    Fields logged:
      timestamp     ISO-8601 UTC
      run_id        pipeline run
      agent         initiating agent
      task_id       task within the run
      tool          PATCH_FILE | WRITE_FILE
      path          target file (project-relative)
      mutation_type search_replace | new_file
      backup_path   backup location before mutation, or null for new files
      diff_lines    number of diff lines (0 for new files)
      diff_preview  first 1 KB of the unified diff
      success       bool
      error         error message if not success
    """
    entry: dict = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id,
        "agent": agent_name,
        "task_id": task_id,
        "tool": tool,
        "path": path,
        "mutation_type": mutation_type,
        "backup_path": backup_path,
        "diff_lines": diff_preview.count("\n") if diff_preview else 0,
        "diff_preview": diff_preview[:1024] if diff_preview else "",
        "success": success,
        "error": error,
    }

    audit_path = runs_dir / run_id / _AUDIT_FILENAME
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if success:
        log.info(
            "Mutation logged: tool=%s path=%s type=%s run_id=%s",
            tool, path, mutation_type, run_id,
        )
    else:
        log.warning(
            "Mutation FAILED: tool=%s path=%s error=%s run_id=%s",
            tool, path, error, run_id,
        )
