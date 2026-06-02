from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class StatusCode(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    SKIPPED = "SKIPPED"
    FEATURE_COMPLETE = "FEATURE_COMPLETE"  # one feature done, more remain — loop back


@dataclass
class IssueItem:
    """A single tracked issue emitted by QA or code_reviewer.

    Agents emit these under `issues_list` in their status block.
    The harness maintains `status` and `attempts` as the resolution loop progresses.
    """
    id: str           # "issue_1", "issue_2" — assigned by agent or auto-generated
    description: str
    severity: str     # "critical" | "major" | "minor"
    found_by: str = ""
    status: str = "pending"   # pending | in_progress | fixed | verified
    attempts: list[dict] = field(default_factory=list)
    # Each attempt: {attempt_num, approach, result, at}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "severity": self.severity,
            "found_by": self.found_by,
            "status": self.status,
            "attempts": [dict(a) for a in self.attempts],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IssueItem":
        return cls(
            id=data.get("id", ""),
            description=data.get("description", ""),
            severity=data.get("severity", "minor"),
            found_by=data.get("found_by", ""),
            status=data.get("status", "pending"),
            attempts=data.get("attempts", []),
        )


@dataclass
class HandoffNote:
    """Structured handoff from one agent to the next.

    Emitted inside the agent's JSON status block under the "handoff" key.
    Captured by the harness and injected as a structured section in the
    next agent's task prompt.
    """
    from_agent: str
    to_agent: str
    decisions: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    notes: str = ""
    # What this agent tried and what happened — used in issue-resolution loops
    # so the next agent doesn't repeat the same failed approach.
    # [{approach: "changed auth.py:45", result: "still 401 — token not invalidated"}]
    attempts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "decisions": self.decisions,
            "requirements": self.requirements,
            "artifacts": self.artifacts,
            "blockers": self.blockers,
            "notes": self.notes,
            "attempts": [dict(a) for a in self.attempts],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HandoffNote":
        return cls(
            from_agent=data.get("from_agent", ""),
            to_agent=data.get("to_agent", ""),
            decisions=data.get("decisions", []),
            requirements=data.get("requirements", []),
            artifacts=data.get("artifacts", []),
            blockers=data.get("blockers", []),
            notes=data.get("notes", ""),
            attempts=data.get("attempts", []),
        )


@dataclass
class StatusBlock:
    run_id: str
    task_id: str
    agent: str
    status: StatusCode
    summary: str = ""
    files_requested: list[str] = field(default_factory=list)
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    command_results: list[str] = field(default_factory=list)
    issues_found: list[str] = field(default_factory=list)
    human_input_required: bool = False
    next_recommended_action: str = ""
    next_agent: str = ""
    handoff: HandoffNote | None = None
    # Structured issue list — emitted by QA/reviewer for issue-resolution loops.
    # Parallel to issues_found (flat strings kept for backward compat).
    issues_list: list[IssueItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "agent": self.agent,
            "status": self.status.value,
            "summary": self.summary,
            "files_requested": self.files_requested,
            "files_created": self.files_created,
            "files_modified": self.files_modified,
            "files_deleted": self.files_deleted,
            "commands_run": self.commands_run,
            "command_results": self.command_results,
            "issues_found": self.issues_found,
            "human_input_required": self.human_input_required,
            "next_recommended_action": self.next_recommended_action,
            "next_agent": self.next_agent,
            "handoff": self.handoff.to_dict() if self.handoff else None,
            "issues_list": [i.to_dict() for i in self.issues_list],
        }


def generate_run_id() -> str:
    now = datetime.now(timezone.utc)
    suffix = uuid.uuid4().hex[:6]
    return f"run_{now.strftime('%Y%m%d_%H%M%S')}_{suffix}"


def generate_task_id(agent_name: str) -> str:
    now = datetime.now(timezone.utc)
    return f"task_{agent_name}_{now.strftime('%Y%m%d_%H%M%S')}"
