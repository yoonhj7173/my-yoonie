from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class ToolBudget:
    """
    Per-agent-run resource budget for the tool loop.

    Checked before every tool call to prevent infinite loops,
    runaway reads, and identical repeated calls.
    """
    max_tool_calls: int = 50
    max_chars_read: int = 500_000   # ~500 KB of text across all reads
    max_files_read: int = 50
    max_malformed_requests: int = 3

    # runtime counters (never set these from outside — use record/record_malformed)
    calls_made: int = 0
    chars_read: int = 0
    files_read: int = 0
    malformed_count: int = 0
    seen_calls: set[str] = field(default_factory=set)
    # Tracks paths written/patched this run. Used to allow re-reads after modifications.
    modified_paths: set[str] = field(default_factory=set)

    # ---------------------------------------------------------------------------
    # Fingerprinting (for duplicate detection)
    # ---------------------------------------------------------------------------

    def fingerprint(self, request: dict) -> str:
        """Stable string key identifying this tool call (ignores 'reason')."""
        tool = request.get("tool", "").upper()
        if tool == "READ_FILE":
            paths = request.get("paths") or (
                [request["path"]] if request.get("path") else []
            )
            return f"READ_FILE:{','.join(sorted(paths))}"
        if tool == "READ_FILE_RANGE":
            return (
                f"READ_FILE_RANGE:{request.get('path', '')}:"
                f"{request.get('start_line', '')}-{request.get('end_line', '')}"
            )
        if tool == "SEARCH_CODE":
            q = request.get("query") or request.get("pattern", "")
            p = request.get("path", ".")
            fp = request.get("file_pattern", "")
            cs = request.get("case_sensitive", False)
            return f"SEARCH_CODE:{q}@{p}:{fp}:cs={cs}"
        if tool == "LIST_FILES":
            return f"LIST_FILES:{request.get('path', '.')}:r={request.get('recursive', False)}"
        if tool == "CODEBASE_MAP":
            return f"CODEBASE_MAP:{request.get('path', '.')}"
        # Fallback: JSON without 'reason' to normalise across wording changes
        reduced = {k: v for k, v in request.items() if k != "reason"}
        return json.dumps(reduced, sort_keys=True, ensure_ascii=False)

    # ---------------------------------------------------------------------------
    # Check / record
    # ---------------------------------------------------------------------------

    def _paths_for_request(self, request: dict) -> list[str]:
        """Return the file path(s) referenced by a read/write request."""
        tool = request.get("tool", "").upper()
        if tool == "READ_FILE":
            return request.get("paths") or (
                [request["path"]] if request.get("path") else []
            )
        if tool in ("READ_FILE_RANGE", "WRITE_FILE", "PATCH_FILE", "CREATE_FILE"):
            p = request.get("path", "")
            return [p] if p else []
        return []

    def check(self, request: dict) -> str | None:
        """
        Return a human-readable reason string if the call should be blocked,
        or None if the call is allowed.

        Call this BEFORE dispatching a tool.
        """
        if self.malformed_count >= self.max_malformed_requests:
            return (
                f"Tool loop halted: {self.malformed_count} malformed requests "
                f"exceeded limit of {self.max_malformed_requests}."
            )
        if self.calls_made >= self.max_tool_calls:
            return f"Budget exceeded: max_tool_calls={self.max_tool_calls} reached."
        if self.chars_read >= self.max_chars_read:
            return (
                f"Budget exceeded: max_chars_read={self.max_chars_read:,} "
                f"({self.chars_read:,} chars consumed so far)."
            )
        if self.files_read >= self.max_files_read:
            return f"Budget exceeded: max_files_read={self.max_files_read} reached."

        fp = self.fingerprint(request)
        if fp in self.seen_calls:
            # Allow re-reads when the file was modified since the last read.
            tool = request.get("tool", "").upper()
            if tool in ("READ_FILE", "READ_FILE_RANGE"):
                paths = self._paths_for_request(request)
                if any(p in self.modified_paths for p in paths):
                    return None  # stale read fingerprint — let it through
            return f"Already executed successfully — do not retry ({fp!r}). The previous call completed. Move on."

        return None

    def record(self, request: dict, chars: int = 0, files: int = 0) -> None:
        """Call this AFTER a tool executes successfully (not on budget blocks)."""
        self.calls_made += 1
        self.chars_read += chars
        self.files_read += files
        fp = self.fingerprint(request)
        self.seen_calls.add(fp)

        # Track mutations and invalidate stale read fingerprints for modified paths.
        tool = request.get("tool", "").upper()
        if tool in ("WRITE_FILE", "PATCH_FILE", "CREATE_FILE"):
            for path in self._paths_for_request(request):
                if path:
                    self.modified_paths.add(path)
                    stale = {
                        f for f in self.seen_calls
                        if (
                            f.startswith("READ_FILE:") and path in f[len("READ_FILE:"):].split(",")
                        ) or f.startswith(f"READ_FILE_RANGE:{path}:")
                    }
                    self.seen_calls -= stale
                    log.debug("ToolBudget: invalidated %d read fingerprint(s) for %s", len(stale), path)

        log.debug(
            "ToolBudget: calls=%d chars=%d files=%d",
            self.calls_made, self.chars_read, self.files_read,
        )

    def record_malformed(self) -> None:
        self.malformed_count += 1
        log.warning("Malformed tool request #%d (limit=%d)", self.malformed_count, self.max_malformed_requests)

    def halted_by_malformed(self) -> bool:
        return self.malformed_count >= self.max_malformed_requests

    # ---------------------------------------------------------------------------
    # Reporting
    # ---------------------------------------------------------------------------

    def summary(self) -> dict:
        return {
            "calls_made": self.calls_made,
            "chars_read": self.chars_read,
            "files_read": self.files_read,
            "malformed_count": self.malformed_count,
            "limits": {
                "max_tool_calls": self.max_tool_calls,
                "max_chars_read": self.max_chars_read,
                "max_files_read": self.max_files_read,
                "max_malformed_requests": self.max_malformed_requests,
            },
        }
