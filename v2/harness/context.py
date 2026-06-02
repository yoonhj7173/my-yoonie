from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from harness.status import StatusBlock

log = logging.getLogger(__name__)

_CONTEXT_FILENAME = "context.json"
_MAX_DECISIONS_IN_PROMPT = 5
_MAX_FILES_IN_PROMPT = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunContext:
    """Pipeline-run-scoped shared state maintained by the harness.

    Created at the start of each pipeline run, updated after every agent
    completes, and injected into every agent's prompt as a structured section.

    Persisted to runs/<run_id>/context.json after each update.
    """
    run_id: str
    stack: dict = field(default_factory=dict)
    # e.g. {"language": "Python", "framework": "FastAPI", "db": "PostgreSQL"}

    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)

    tests_status: dict = field(default_factory=dict)
    # e.g. {"written": 12, "passing": 12, "failing": 0}

    decisions: list[dict] = field(default_factory=list)
    # e.g. [{"agent": "system_architect", "decision": "monolith", "at": "ISO"}]

    # ── Persistence ──────────────────────────────────────────────────────────

    @classmethod
    def load_or_create(cls, run_id: str, runs_dir: Path) -> "RunContext":
        path = runs_dir / run_id / _CONTEXT_FILENAME
        if path.exists():
            try:
                return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:
                log.warning("Could not load RunContext from %s: %s — starting fresh", path, exc)
        return cls(run_id=run_id)

    def save(self, runs_dir: Path) -> None:
        path = runs_dir / self.run_id / _CONTEXT_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        log.debug("RunContext saved  run_id=%s  files=%d  decisions=%d",
                  self.run_id, len(self.files_created), len(self.decisions))

    # ── Update ───────────────────────────────────────────────────────────────

    def update_from_status_block(self, block: StatusBlock) -> None:
        """Absorb data from a completed agent's StatusBlock."""
        # Accumulate created/modified files (no duplicates)
        for f in block.files_created:
            if f not in self.files_created:
                self.files_created.append(f)
        for f in block.files_modified:
            if f not in self.files_modified:
                self.files_modified.append(f)

        # Absorb decisions from HandoffNote
        if block.handoff:
            for decision in block.handoff.decisions:
                entry = {"agent": block.agent, "decision": decision, "at": _now_iso()}
                # Avoid exact duplicates (same agent + decision text)
                if not any(
                    d["agent"] == entry["agent"] and d["decision"] == entry["decision"]
                    for d in self.decisions
                ):
                    self.decisions.append(entry)

    # ── Prompt injection ─────────────────────────────────────────────────────

    def to_prompt_section(self) -> str:
        """Render a markdown section to inject into agent prompts.

        Only included when there is meaningful content (files or decisions).
        Returns an empty string when the context is still empty (e.g. first agent).
        """
        lines = ["## Run Context (Shared State)"]

        if self.stack:
            stack_str = " / ".join(f"{v}" for v in self.stack.values() if v)
            lines.append(f"**Stack:** {stack_str}")

        if self.files_created:
            shown = self.files_created[:_MAX_FILES_IN_PROMPT]
            more = len(self.files_created) - len(shown)
            suffix = f" (+{more} more)" if more else ""
            lines.append(f"**Files Created:** {', '.join(shown)}{suffix}")

        if self.files_modified:
            shown = self.files_modified[:_MAX_FILES_IN_PROMPT]
            more = len(self.files_modified) - len(shown)
            suffix = f" (+{more} more)" if more else ""
            lines.append(f"**Files Modified:** {', '.join(shown)}{suffix}")

        if self.tests_status:
            t = self.tests_status
            lines.append(
                f"**Tests:** {t.get('written', 0)} written, "
                f"{t.get('passing', 0)} passing, "
                f"{t.get('failing', 0)} failing"
            )

        if self.decisions:
            recent = self.decisions[-_MAX_DECISIONS_IN_PROMPT:]
            lines.append("**Decisions:**")
            for d in recent:
                lines.append(f"- [{d['agent']}] {d['decision']}")

        return "\n".join(lines)

    def is_empty(self) -> bool:
        return (
            not self.stack
            and not self.files_created
            and not self.files_modified
            and not self.tests_status
            and not self.decisions
        )

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "stack": dict(self.stack),
            "files_created": list(self.files_created),
            "files_modified": list(self.files_modified),
            "tests_status": dict(self.tests_status),
            "decisions": [dict(d) for d in self.decisions],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunContext":
        return cls(
            run_id=data.get("run_id", ""),
            stack=data.get("stack", {}),
            files_created=data.get("files_created", []),
            files_modified=data.get("files_modified", []),
            tests_status=data.get("tests_status", {}),
            decisions=data.get("decisions", []),
        )
