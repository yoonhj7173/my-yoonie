from __future__ import annotations

"""
Tool dispatcher: parse → validate → budget-check → execute.

Parser is deliberately defensive:
  - Handles malformed JSON, partial JSON, nested braces, multiple blocks.
  - Never silently ignores errors — all problems are returned as structured results.
  - EXEC_COMMAND / PATCH_FILE / WRITE_FILE are gated until filesystem mutation
    safety (diff previews, approval hooks, audit log) is implemented.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from harness.tools.browser import (
    browser_click,
    browser_eval,
    browser_fill,
    browser_get_text,
    browser_navigate,
    browser_screenshot,
)
from harness.tools.budget import ToolBudget
from harness.tools.codebase_map import format_codebase_map, get_or_build_codebase_map
from harness.tools.exec_cmd import dry_run_preview, exec_command
from harness.tools.file_ops import ToolError, list_files, read_file
from harness.tools.mutations import MutationContext, patch_file, write_file
from harness.tools.permissions import check_tool_permission
from harness.tools.search import search_code

log = logging.getLogger(__name__)

# All tools currently implemented. EXEC_COMMAND is gated by allowlist in exec_cmd.py.
_NOT_IMPLEMENTED_TOOLS: frozenset[str] = frozenset()

_KNOWN_TOOLS = frozenset({
    "LIST_FILES",
    "READ_FILE",
    "READ_FILE_RANGE",
    "SEARCH_CODE",
    "CODEBASE_MAP",
    "PATCH_FILE",
    "WRITE_FILE",
    "CREATE_FILE",   # alias for WRITE_FILE — some agents use this name
    "EXEC_COMMAND",
    "BROWSER_NAVIGATE",
    "BROWSER_SCREENSHOT",
    "BROWSER_CLICK",
    "BROWSER_FILL",
    "BROWSER_EVAL",
    "BROWSER_GET_TEXT",
})

# Tools that are aliases of other tools — normalised before dispatch.
_TOOL_ALIASES: dict[str, str] = {
    "CREATE_FILE": "WRITE_FILE",
}

# Matches ``` or ```json code fences (non-greedy content capture).
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

@dataclass
class ParseResult:
    requests: list[dict]   # valid, ready to dispatch
    errors: list[dict]     # structured parse / validation errors
    blocks_found: int      # total code blocks that looked like tool_requests


def parse_tool_requests(text: str) -> ParseResult:
    """
    Robustly extract tool_requests from agent output text.

    Handles:
      - malformed / partial JSON
      - nested braces
      - multiple blocks (all are processed; valid requests are merged)
      - hallucinated tool names  (returned as structured errors)
      - missing required fields  (returned as structured errors)
      - non-list tool_requests   (returned as structured error)

    Returns a ParseResult — never raises.
    """
    valid_requests: list[dict] = []
    errors: list[dict] = []
    blocks_with_tool_requests = 0

    for block_idx, match in enumerate(_CODE_FENCE_RE.finditer(text)):
        raw = match.group(1).strip()
        if not raw or '"tool_requests"' not in raw:
            continue

        blocks_with_tool_requests += 1

        # ── Parse JSON ────────────────────────────────────────────────────────
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append({
                "error_type": "json_parse_error",
                "block_index": block_idx,
                "detail": str(exc),
                "raw_preview": raw[:300] + ("…" if len(raw) > 300 else ""),
            })
            log.warning("Block %d: JSON parse error: %s", block_idx, exc)
            continue

        # ── Structural validation ─────────────────────────────────────────────
        if not isinstance(data, dict):
            errors.append({
                "error_type": "structure_error",
                "block_index": block_idx,
                "detail": f"Expected JSON object, got {type(data).__name__}",
            })
            continue

        tool_reqs = data.get("tool_requests")
        if not isinstance(tool_reqs, list):
            errors.append({
                "error_type": "structure_error",
                "block_index": block_idx,
                "detail": (
                    f"'tool_requests' must be an array, "
                    f"got {type(tool_reqs).__name__ if tool_reqs is not None else 'null'}"
                ),
            })
            continue

        # ── Per-request validation ────────────────────────────────────────────
        for req_idx, req in enumerate(tool_reqs):
            loc = f"block[{block_idx}].request[{req_idx}]"

            if not isinstance(req, dict):
                errors.append({
                    "error_type": "request_error",
                    "location": loc,
                    "detail": f"Request must be an object, got {type(req).__name__}",
                })
                continue

            tool_name = req.get("tool")
            if not tool_name or not isinstance(tool_name, str):
                errors.append({
                    "error_type": "missing_field",
                    "location": loc,
                    "detail": "Request is missing the required 'tool' field.",
                    "request": req,
                })
                continue

            tool_upper = tool_name.upper()
            if tool_upper not in _KNOWN_TOOLS:
                errors.append({
                    "error_type": "unknown_tool",
                    "location": loc,
                    "tool": tool_name,
                    "detail": (
                        f"Unknown tool '{tool_name}'. "
                        f"Available: {', '.join(sorted(_KNOWN_TOOLS - _NOT_IMPLEMENTED_TOOLS))}."
                    ),
                })
                continue

            # Normalize tool name: case-insensitive + resolve aliases
            req = dict(req)
            req["tool"] = _TOOL_ALIASES.get(tool_upper, tool_upper)
            valid_requests.append(req)

    return ParseResult(
        requests=valid_requests,
        errors=errors,
        blocks_found=blocks_with_tool_requests,
    )


# ---------------------------------------------------------------------------
# Single-request dispatcher
# ---------------------------------------------------------------------------

def _dispatch_one(
    request: dict,
    project_root: Path,
    mutation_ctx: MutationContext | None = None,
) -> tuple[dict, int, int]:
    """
    Execute one validated tool request.

    Returns (result_dict, chars_consumed, files_consumed).
    chars_consumed / files_consumed are used to update ToolBudget counters.
    """
    tool = request["tool"]   # already uppercased by parser

    # ── LIST_FILES ────────────────────────────────────────────────────────────
    if tool == "LIST_FILES":
        path = request.get("path", ".")
        recursive = bool(request.get("recursive", False))
        try:
            result = list_files(path, project_root, recursive=recursive)
        except ToolError as exc:
            return {"tool": tool, "status": "error", "error": str(exc)}, 0, 0
        chars = sum(len(e["name"]) for e in result["entries"])
        return {"tool": tool, "status": "ok", **result}, chars, 0

    # ── READ_FILE ─────────────────────────────────────────────────────────────
    if tool == "READ_FILE":
        paths = request.get("paths") or (
            [request["path"]] if request.get("path") else []
        )
        if not paths:
            return {"tool": tool, "status": "error", "error": "No paths provided."}, 0, 0
        files_out = []
        total_chars = 0
        for p in paths:
            try:
                r = read_file(p, project_root)
                total_chars += len(r["content"])
                files_out.append({"status": "ok", **r})
            except ToolError as exc:
                files_out.append({"path": p, "status": "error", "error": str(exc)})
        return {"tool": tool, "files": files_out}, total_chars, len(paths)

    # ── READ_FILE_RANGE ───────────────────────────────────────────────────────
    if tool == "READ_FILE_RANGE":
        path = request.get("path")
        if not path:
            return {"tool": tool, "status": "error", "error": "No path provided."}, 0, 0
        try:
            r = read_file(
                path,
                project_root,
                start_line=request.get("start_line"),
                end_line=request.get("end_line"),
            )
        except ToolError as exc:
            return {"tool": tool, "status": "error", "error": str(exc)}, 0, 0
        return {"tool": tool, "status": "ok", **r}, len(r["content"]), 1

    # ── SEARCH_CODE ───────────────────────────────────────────────────────────
    if tool == "SEARCH_CODE":
        pattern = request.get("query") or request.get("pattern", "")
        if not pattern:
            return {"tool": tool, "status": "error", "error": "No query/pattern provided."}, 0, 0
        search_path = request.get("path", ".")
        file_pattern = request.get("file_pattern")
        case_sensitive = bool(request.get("case_sensitive", False))
        try:
            r = search_code(
                pattern=pattern,
                project_root=project_root,
                path=search_path,
                file_pattern=file_pattern,
                case_sensitive=case_sensitive,
            )
        except ToolError as exc:
            return {"tool": tool, "status": "error", "error": str(exc)}, 0, 0
        chars = sum(len(m["line"]) for m in r["matches"])
        return {"tool": tool, "status": "ok", **r}, chars, 0

    # ── CODEBASE_MAP ─────────────────────────────────────────────────────────
    if tool == "CODEBASE_MAP":
        search_path = request.get("path", ".")
        target_root = project_root / search_path
        cache_dir = project_root / "cache"
        cmap = get_or_build_codebase_map(target_root, cache_dir=cache_dir)
        summary = format_codebase_map(cmap)
        # Persist to runs/<run_id>/codebase-map.md when running in a pipeline context
        if mutation_ctx is not None:
            run_map_path = mutation_ctx.runs_dir / mutation_ctx.run_id / "codebase-map.md"
            run_map_path.parent.mkdir(parents=True, exist_ok=True)
            run_map_path.write_text(summary, encoding="utf-8")
            log.debug("Codebase map saved to %s", run_map_path)
        return {
            "tool": tool,
            "status": "ok",
            "summary": summary,
        }, len(summary), 0

    # ── PATCH_FILE ────────────────────────────────────────────────────────────
    if tool == "PATCH_FILE":
        if mutation_ctx is None:
            return {
                "tool": tool,
                "status": "error",
                "error": "PATCH_FILE requires a mutation context (no run context available).",
            }, 0, 0
        try:
            result = patch_file(request, mutation_ctx)
            return result, 0, 0
        except ToolError as exc:
            return {"tool": tool, "status": "error", "error": str(exc)}, 0, 0

    # ── WRITE_FILE ────────────────────────────────────────────────────────────
    if tool == "WRITE_FILE":
        if mutation_ctx is None:
            return {
                "tool": tool,
                "status": "error",
                "error": "WRITE_FILE requires a mutation context (no run context available).",
            }, 0, 0
        try:
            result = write_file(request, mutation_ctx)
            return result, 0, 0
        except ToolError as exc:
            return {"tool": tool, "status": "error", "error": str(exc)}, 0, 0

    # ── EXEC_COMMAND ──────────────────────────────────────────────────────────
    if tool == "EXEC_COMMAND":
        if mutation_ctx is None:
            return {
                "tool": tool,
                "status": "error",
                "error": "EXEC_COMMAND requires a run context (no mutation context available).",
            }, 0, 0
        try:
            result = exec_command(request, mutation_ctx)
            return result, 0, 0
        except ToolError as exc:
            return {"tool": tool, "status": "error", "error": str(exc)}, 0, 0

    # ── BROWSER_* ─────────────────────────────────────────────────────────────
    if tool in {"BROWSER_NAVIGATE", "BROWSER_SCREENSHOT", "BROWSER_CLICK",
                "BROWSER_FILL", "BROWSER_EVAL", "BROWSER_GET_TEXT"}:
        if mutation_ctx is None:
            return {
                "tool": tool,
                "status": "error",
                "error": "Browser tools require a run context (no mutation context available).",
            }, 0, 0
        run_id = mutation_ctx.run_id
        runs_dir = mutation_ctx.runs_dir
        try:
            if tool == "BROWSER_NAVIGATE":
                result = browser_navigate(request, run_id)
            elif tool == "BROWSER_SCREENSHOT":
                result = browser_screenshot(request, run_id, runs_dir)
            elif tool == "BROWSER_CLICK":
                result = browser_click(request, run_id)
            elif tool == "BROWSER_FILL":
                result = browser_fill(request, run_id)
            elif tool == "BROWSER_EVAL":
                result = browser_eval(request, run_id)
            else:  # BROWSER_GET_TEXT
                result = browser_get_text(request, run_id)
            chars = len(str(result.get("result", result.get("text", ""))))
            return result, chars, 0
        except ToolError as exc:
            return {"tool": tool, "status": "error", "error": str(exc)}, 0, 0

    # Should not reach here — parser already filters unknown tools.
    return {"tool": tool, "status": "error", "error": f"Unhandled tool: '{tool}'."}, 0, 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def dispatch_tool_requests(
    requests: list[dict],
    project_root: Path,
    budget: ToolBudget | None = None,
    mutation_ctx: MutationContext | None = None,
) -> list[dict]:
    """
    Execute all validated tool requests and return a list of result dicts.

    Budget is checked before each call:
      - exceeded limits → blocked result (tool call skipped)
      - duplicate call  → blocked result (tool call skipped)

    mutation_ctx is required for PATCH_FILE and WRITE_FILE; read-only tools
    ignore it. If None, mutation tools return a structured error.

    If budget is None, a default ToolBudget is created (useful for tests).
    """
    if budget is None:
        budget = ToolBudget()

    results: list[dict] = []
    for req in requests:
        tool = req.get("tool", "?")

        # ── Permission gate (agent tool policy) ──────────────────────────────
        if mutation_ctx is not None:
            try:
                check_tool_permission(mutation_ctx.agent_name, tool)
            except ToolError as exc:
                log.warning("Permission denied: agent=%s tool=%s", mutation_ctx.agent_name, tool)
                results.append({
                    "tool": tool,
                    "status": "permission_denied",
                    "error": str(exc),
                })
                continue

        # ── Budget / duplicate gate ───────────────────────────────────────────
        block_reason = budget.check(req)
        if block_reason:
            log.warning("Tool call blocked: %s | %s", tool, block_reason)
            results.append({
                "tool": tool,
                "status": "blocked",
                "reason": block_reason,
            })
            continue

        # ── Execute ──────────────────────────────────────────────────────────
        log.info("Tool call: %s", tool)
        try:
            result, chars, files = _dispatch_one(req, project_root, mutation_ctx)
        except Exception as exc:
            log.exception("Unexpected error in tool %s", tool)
            result = {"tool": tool, "status": "error", "error": str(exc)}
            chars, files = 0, 0

        budget.record(req, chars=chars, files=files)
        results.append(result)

    return results


def format_tool_results_for_prompt(results: list[dict]) -> str:
    """Serialize tool results into structured text for the next LLM turn."""
    payload = json.dumps({"tool_results": results}, indent=2, ensure_ascii=False)
    return f"## Tool Results\n\n```json\n{payload}\n```\n"
