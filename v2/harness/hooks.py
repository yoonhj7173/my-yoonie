from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event name constants
# ---------------------------------------------------------------------------

BEFORE_AGENT_RUN = "before_agent_run"
AFTER_AGENT_RUN = "after_agent_run"
BEFORE_PROVIDER_CALL = "before_provider_call"
AFTER_PROVIDER_CALL = "after_provider_call"
BEFORE_TOOL_EXECUTION = "before_tool_execution"   # Phase 4+
AFTER_TOOL_EXECUTION = "after_tool_execution"     # Phase 4+
ON_STATUS_CHANGE = "on_status_change"
ON_HUMAN_APPROVAL_REQUIRED = "on_human_approval_required"  # Phase 4+
ON_COMMAND_FAILURE = "on_command_failure"                  # Phase 5+
ON_ERROR = "on_error"
ON_MAX_ATTEMPTS_REACHED = "on_max_attempts_reached"        # Phase 6+
ON_ESCALATION = "on_escalation"                            # Phase 6+
ON_WORKFLOW_COMPLETE = "on_workflow_complete"              # Phase 6+
ON_WORKFLOW_PAUSED = "on_workflow_paused"                  # Phase 6+
ON_WORKFLOW_RESUMED = "on_workflow_resumed"                # Phase 6+

Handler = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# HookSystem
# ---------------------------------------------------------------------------

class HookSystem:
    """
    Thin synchronous hook layer.

    Usage:
        hooks.on("after_agent_run", my_handler)
        hooks.emit("after_agent_run", {"agent": "product_manager", ...})

    Handlers are called in registration order.
    A handler that raises does not prevent other handlers from running.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def on(self, event_name: str, handler: Handler) -> None:
        self._handlers[event_name].append(handler)

    def emit(self, event_name: str, payload: dict[str, Any]) -> None:
        for handler in self._handlers.get(event_name, []):
            try:
                handler(payload)
            except Exception as exc:
                log.error("Hook handler for %r raised: %s", event_name, exc)


# ---------------------------------------------------------------------------
# Module-level default instance
# ---------------------------------------------------------------------------

hooks = HookSystem()
