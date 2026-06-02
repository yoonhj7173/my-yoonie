from __future__ import annotations

# ---------------------------------------------------------------------------
# Linear happy-path routing
# ---------------------------------------------------------------------------

_LINEAR_NEXT: dict[str, str] = {
    "product_manager":   "system_architect",
    "system_architect":  "software_engineer",
    "software_engineer": "qa_engineer",
    "qa_engineer":       "code_reviewer",
    "code_reviewer":     "devops_engineer",
    "devops_engineer":   "",
    "debugger_engineer": "",   # resume point resolved via debugger_from (see below)
}

AGENT_ORDER: list[str] = [
    "product_manager",
    "system_architect",
    "software_engineer",
    "qa_engineer",
    "code_reviewer",
    "devops_engineer",
]

ALL_AGENTS: set[str] = set(_LINEAR_NEXT.keys()) | {"advisor"}

# ---------------------------------------------------------------------------
# Debugger routing
#
# When code_reviewer or qa_engineer returns BLOCKED (needs debugger help),
# the pipeline routes to debugger_engineer. After debugging, routes back
# to the agent that requested it.
# ---------------------------------------------------------------------------

_DEBUGGER_ROUTE: dict[str, str] = {
    "code_reviewer": "debugger_engineer",
    "qa_engineer":   "debugger_engineer",
}

_DEBUGGER_RESUME: dict[str, str] = {
    "code_reviewer": "code_reviewer",
    "qa_engineer":   "qa_engineer",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def next_agent(
    current_agent: str,
    status: str,
    recovery_from: str = "",
    debugger_from: str = "",
) -> str | None:
    """
    Return the next agent given current agent and its run status.

    Routing rules (state machine is authoritative — LLM cannot override):
      1. FAILED → pipeline stops (agents self-loop via max_attempts before failing)
      2. debugger_engineer SUCCESS → resume from _DEBUGGER_RESUME[debugger_from]
      3. Otherwise → linear _LINEAR_NEXT
    """
    if status == "FAILED":
        return None

    # Feature-by-feature loop: software_engineer signals one feature done, more remain
    if status == "FEATURE_COMPLETE" and current_agent == "software_engineer":
        return "software_engineer"

    if current_agent == "debugger_engineer":
        # Route back to the agent that requested debugging.
        resume = _DEBUGGER_RESUME.get(debugger_from, "code_reviewer")
        return resume if resume else None

    nxt = _LINEAR_NEXT.get(current_agent, "")
    return nxt if nxt else None


def failure_is_recoverable(agent: str) -> bool:
    """Always False — recovery_engineer removed; agents self-heal via max_attempts."""
    return False


def debugger_target(agent: str) -> str | None:
    """Return the debugger agent to route to when agent returns BLOCKED, or None."""
    return _DEBUGGER_ROUTE.get(agent)


_AGENT_ALIASES: dict[str, str] = {
    "pm":    "product_manager",
    "arch":  "system_architect",
    "swe":   "software_engineer",
    "qa":    "qa_engineer",
    "cr":    "code_reviewer",
    "devops": "devops_engineer",
    "dbg":   "debugger_engineer",
}


def resolve_agent(name: str) -> str:
    """Resolve alias → canonical name. Returns name unchanged if not an alias."""
    return _AGENT_ALIASES.get(name, name)


def is_known_agent(name: str) -> bool:
    return resolve_agent(name) in ALL_AGENTS
