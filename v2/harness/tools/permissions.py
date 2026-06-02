from __future__ import annotations

"""
Agent tool permission table.

Permissions are enforced at the dispatcher layer (not just in LLM prompts).
This module defines which tools each agent is allowed to call, with defaults
derived from the agent's role.

Design:
  - Read-only agents (product_manager, code_reviewer) cannot mutate files or run commands.
  - QA can run commands but cannot modify source files (it only verifies).
  - Engineers and recovery_engineer have full access (they are the actors).
  - system_architect can write design documents but not execute commands.

The YAML `allowed_actions` / `forbidden_actions` fields describe high-level
responsibilities; this module provides the low-level tool-name enforcement.
"""

from harness.tools.file_ops import ToolError

# All known tool names (must stay in sync with dispatcher._KNOWN_TOOLS)
_ALL_TOOLS: frozenset[str] = frozenset({
    "LIST_FILES",
    "READ_FILE",
    "READ_FILE_RANGE",
    "SEARCH_CODE",
    "CODEBASE_MAP",
    "PATCH_FILE",
    "WRITE_FILE",
    "CREATE_FILE",    # alias for WRITE_FILE, resolved by dispatcher
    "EXEC_COMMAND",
    "BROWSER_NAVIGATE",
    "BROWSER_SCREENSHOT",
    "BROWSER_CLICK",
    "BROWSER_FILL",
    "BROWSER_EVAL",
    "BROWSER_GET_TEXT",
})

_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "LIST_FILES",
    "READ_FILE",
    "READ_FILE_RANGE",
    "SEARCH_CODE",
    "CODEBASE_MAP",
})

_BROWSER_TOOLS: frozenset[str] = frozenset({
    "BROWSER_NAVIGATE",
    "BROWSER_SCREENSHOT",
    "BROWSER_CLICK",
    "BROWSER_FILL",
    "BROWSER_EVAL",
    "BROWSER_GET_TEXT",
})

# ---------------------------------------------------------------------------
# Per-agent tool permissions
#
# Keys: agent name
# Values: set of allowed tool names ("*" means all tools)
#
# Not listed → defaults to read-only tools only.
# ---------------------------------------------------------------------------

_AGENT_ALLOWED_TOOLS: dict[str, frozenset[str] | str] = {
    # ── Read-only roles ───────────────────────────────────────────────────────
    "product_manager": _READ_ONLY_TOOLS,

    # ── Architect: may write design documents ─────────────────────────────────
    "system_architect": _READ_ONLY_TOOLS | frozenset({"WRITE_FILE", "CREATE_FILE"}),

    # ── QA: runs tests + writes test files (not product code) ─────────────────
    "qa_engineer": (
        _READ_ONLY_TOOLS
        | _BROWSER_TOOLS
        | frozenset({"EXEC_COMMAND", "WRITE_FILE", "CREATE_FILE", "PATCH_FILE"})
    ),

    # ── Code reviewer: fixes minor issues inline + runtime testing ────────────
    "code_reviewer": (
        _READ_ONLY_TOOLS
        | _BROWSER_TOOLS
        | frozenset({"EXEC_COMMAND", "PATCH_FILE", "WRITE_FILE", "CREATE_FILE"})
    ),

    # ── Engineers: full access ─────────────────────────────────────────────────
    "software_engineer": "*",
    "devops_engineer": "*",
    "debugger_engineer": "*",
}


def check_tool_permission(agent_name: str, tool: str) -> None:
    """
    Raise ToolError if agent_name is not permitted to use tool.

    Called in the dispatcher before executing each tool.
    Unknown agents default to read-only access.
    """
    allowed = _AGENT_ALLOWED_TOOLS.get(agent_name)

    if allowed == "*":
        return  # full access

    if allowed is None:
        # Unknown agent — read-only by default
        allowed = _READ_ONLY_TOOLS

    if tool not in allowed:
        raise ToolError(
            f"Permission denied: agent '{agent_name}' is not allowed to use {tool}. "
            f"Allowed tools for this agent: {', '.join(sorted(allowed))}."
        )


def get_allowed_tools(agent_name: str) -> frozenset[str] | str:
    """Return the allowed tool set for an agent ('*' means all)."""
    return _AGENT_ALLOWED_TOOLS.get(agent_name, _READ_ONLY_TOOLS)
