from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.context import RunContext

log = logging.getLogger(__name__)

_MEMORY_PATH = "context/memory.json"
_MEMORY_VERSION = 1
_INJECT_LIMIT = 3      # most recent entries injected into task
_FACTS_PER_ENTRY = 3   # LLM extracts exactly this many facts per run

_EXTRACTION_SYSTEM = (
    "You are a memory extractor. "
    "Given a completed pipeline run, output a JSON object and nothing else."
)

_EXTRACTION_TEMPLATE = """\
A pipeline run just completed. Extract exactly {n} key facts about this project \
that would help a future pipeline run — focus on architectural decisions, tech stack, \
patterns, and constraints. Ignore one-off errors or run-specific details.

Task: {task}
Agents completed: {agents}
Stack: {stack}
Files created: {files}

Reply with ONLY valid JSON, no markdown fences:
{{"task_summary": "<one line>", "key_facts": ["fact1", "fact2", "fact3"], "tags": ["tag1", "tag2"]}}"""


# ---------------------------------------------------------------------------
# MemoryEntry
# ---------------------------------------------------------------------------

@dataclass
class MemoryEntry:
    timestamp: str
    run_id: str
    task_summary: str
    key_facts: list[str]
    stack: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "task_summary": self.task_summary,
            "key_facts": list(self.key_facts),
            "stack": dict(self.stack),
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryEntry":
        return cls(
            timestamp=data.get("timestamp", ""),
            run_id=data.get("run_id", ""),
            task_summary=data.get("task_summary", ""),
            key_facts=data.get("key_facts", []),
            stack=data.get("stack", {}),
            tags=data.get("tags", []),
        )


# ---------------------------------------------------------------------------
# Storage backends
# ---------------------------------------------------------------------------

class JsonMemoryBackend:
    """Default backend — stores entries as a flat JSON array in context/memory.json."""

    def load(self, project_root: Path) -> list[MemoryEntry]:
        path = project_root / _MEMORY_PATH
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [MemoryEntry.from_dict(e) for e in data.get("entries", [])]
        except Exception as exc:
            log.warning("JsonMemoryBackend: could not load %s: %s", path, exc)
            return []

    def store(self, entry: MemoryEntry, project_root: Path) -> None:
        path = project_root / _MEMORY_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {"version": _MEMORY_VERSION, "entries": []}
        else:
            data = {"version": _MEMORY_VERSION, "entries": []}
        data["entries"].append(entry.to_dict())
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log.info("JsonMemoryBackend: stored entry run_id=%s", entry.run_id)


class Mem0MemoryBackend:
    """mem0 backend — local Qdrant vector store + OpenAI embedder.

    Requires:
      - OPENAI_API_KEY env var (for embeddings)
      - pip install mem0ai  (already a project dependency)

    Stored in <project_root>/.mem0/ — separate from context/ artifacts.

    Each MemoryEntry is stored as individual fact messages tagged with run_id
    so they can be reconstructed on load.
    """

    # Module-level cache keyed by project_root str — avoid re-init on every call
    _instances: dict[str, "Mem0MemoryBackend"] = {}

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        self._user_id = project_root.resolve().name
        self._mem0 = self._build_memory(project_root)

    @classmethod
    def get(cls, project_root: Path) -> "Mem0MemoryBackend":
        key = str(project_root.resolve())
        if key not in cls._instances:
            cls._instances[key] = cls(project_root)
        return cls._instances[key]

    @staticmethod
    def _build_memory(project_root: Path):
        from mem0 import Memory  # noqa: PLC0415
        from mem0.configs.base import (  # noqa: PLC0415
            MemoryConfig, VectorStoreConfig, LlmConfig, EmbedderConfig,
        )

        openai_key = os.environ.get("OPENAI_API_KEY", "")
        mem0_dir = project_root / ".mem0"
        mem0_dir.mkdir(exist_ok=True)

        config = MemoryConfig(
            vector_store=VectorStoreConfig(
                provider="qdrant",
                config={
                    "path": str(mem0_dir / "qdrant"),
                    "on_disk": True,
                    "collection_name": "harness_memory",
                },
            ),
            llm=LlmConfig(
                provider="openai",
                config={"api_key": openai_key, "model": "gpt-4o-mini"},
            ),
            embedder=EmbedderConfig(
                provider="openai",
                config={"api_key": openai_key, "model": "text-embedding-3-small"},
            ),
            history_db_path=str(mem0_dir / "history.db"),
        )
        return Memory(config=config)

    def load(self, project_root: Path) -> list[MemoryEntry]:
        try:
            results = self._mem0.get_all(
                filters={"user_id": self._user_id},
                top_k=100,
            )
            raw = results.get("results", []) if isinstance(results, dict) else (results or [])
        except Exception as exc:
            log.warning("Mem0MemoryBackend: get_all failed: %s", exc)
            return []

        # Reconstruct MemoryEntry objects from stored facts grouped by run_id
        return _reconstruct_entries_from_mem0(raw)

    def store(self, entry: MemoryEntry, project_root: Path) -> None:
        # Store each key_fact as a separate mem0 memory message.
        # infer=False: bypass mem0's LLM extraction — we already extracted the facts.
        # Tag with metadata so we can reconstruct MemoryEntry on load.
        messages = [
            {"role": "user", "content": fact}
            for fact in entry.key_facts
        ]
        metadata = {
            "run_id": entry.run_id,
            "timestamp": entry.timestamp,
            "task_summary": entry.task_summary[:120],
            "stack_json": json.dumps(entry.stack),
            "tags": ",".join(entry.tags),
        }
        try:
            self._mem0.add(
                messages,
                user_id=self._user_id,
                metadata=metadata,
                infer=False,
            )
            log.info("Mem0MemoryBackend: stored %d facts for run_id=%s",
                     len(entry.key_facts), entry.run_id)
        except Exception as exc:
            log.warning("Mem0MemoryBackend: store failed for run_id=%s: %s",
                        entry.run_id, exc)
            raise


def _reconstruct_entries_from_mem0(raw: list[dict]) -> list[MemoryEntry]:
    """Group mem0 results by run_id and rebuild MemoryEntry objects."""
    # raw items: {"id": ..., "memory": "fact text", "metadata": {...}}
    by_run: dict[str, dict] = {}
    for item in raw:
        meta = item.get("metadata") or {}
        run_id = meta.get("run_id", "unknown")
        if run_id not in by_run:
            by_run[run_id] = {
                "run_id": run_id,
                "timestamp": meta.get("timestamp", ""),
                "task_summary": meta.get("task_summary", ""),
                "stack": json.loads(meta.get("stack_json", "{}")),
                "tags": [t for t in meta.get("tags", "").split(",") if t],
                "key_facts": [],
            }
        text = item.get("memory", "")
        if text:
            by_run[run_id]["key_facts"].append(text)

    entries = [MemoryEntry.from_dict(v) for v in by_run.values()]
    # Sort by timestamp ascending so most recent is last (matches JSON backend order)
    entries.sort(key=lambda e: e.timestamp)
    return entries


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def _load_backend_config(project_root: Path) -> str:
    """Return 'json' (default) or 'mem0' from config/harness.yaml."""
    try:
        import yaml  # noqa: PLC0415
        cfg_path = project_root / "config" / "harness.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            return (cfg.get("memory") or {}).get("backend", "json")
    except Exception:
        pass
    return "json"


def _get_backend(project_root: Path) -> JsonMemoryBackend | Mem0MemoryBackend:
    backend_name = _load_backend_config(project_root)
    if backend_name == "mem0":
        if not os.environ.get("OPENAI_API_KEY"):
            log.warning(
                "memory.backend=mem0 but OPENAI_API_KEY is not set — "
                "falling back to json backend"
            )
            return JsonMemoryBackend()
        try:
            return Mem0MemoryBackend.get(project_root)
        except Exception as exc:
            log.warning("Mem0MemoryBackend init failed (%s) — falling back to json", exc)
            return JsonMemoryBackend()
    return JsonMemoryBackend()


# ---------------------------------------------------------------------------
# ProjectMemory — public API (unchanged from Phase 3)
# ---------------------------------------------------------------------------

class ProjectMemory:

    @classmethod
    def load(cls, project_root: Path) -> list[MemoryEntry]:
        return _get_backend(project_root).load(project_root)

    @classmethod
    def append(cls, entry: MemoryEntry, project_root: Path) -> None:
        _get_backend(project_root).store(entry, project_root)

    @classmethod
    def extract_from_run(
        cls,
        run_id: str,
        task: str,
        completed: list[dict],
        run_context: "RunContext",
        provider_name: str,
        project_root: Path,
    ) -> MemoryEntry | None:
        """Call a cheap LLM to extract key facts from a completed run.

        Returns None on any failure — callers must treat this as non-fatal.
        """
        from harness.models.config import load_model_config  # noqa: PLC0415
        from harness.runner import _get_provider              # noqa: PLC0415

        agents_ran = [s["agent"] for s in completed]
        files_sample = run_context.files_created[:20]
        stack = run_context.stack

        prompt = _EXTRACTION_TEMPLATE.format(
            n=_FACTS_PER_ENTRY,
            task=task[:400],
            agents=", ".join(agents_ran),
            stack=stack or "(unknown)",
            files=", ".join(files_sample) or "(none)",
        )

        try:
            model_cfg = load_model_config(project_root / "config" / "models.yaml")
            resolved_model = model_cfg.resolve("cheap")
        except Exception as exc:
            log.warning("ProjectMemory: could not load model config: %s", exc)
            return None

        prompt_package = {
            "agent_name": "memory_extractor",
            "system_prompt": _EXTRACTION_SYSTEM,
            "user_message": prompt,
            "run_id": run_id,
            "task_id": "memory_extraction",
        }
        model_config = {
            "provider": "openrouter",
            "tier": "cheap",
            "model": resolved_model,
            "temperature": 0.0,
        }

        try:
            provider = _get_provider(provider_name)
            result = provider.generate(prompt_package, model_config)
        except Exception as exc:
            log.warning("ProjectMemory: provider call failed: %s", exc)
            return None

        if result.status == "error":
            log.warning("ProjectMemory: provider error: %s", result.error)
            return None

        return cls._parse_extraction(result.text, run_id, stack)

    @classmethod
    def _parse_extraction(
        cls, text: str, run_id: str, stack: dict
    ) -> MemoryEntry | None:
        """Parse the LLM's JSON response into a MemoryEntry."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            cleaned = "\n".join(
                ln for ln in lines if not ln.startswith("```")
            ).strip()

        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start == -1 or end == 0:
            log.warning("ProjectMemory: no JSON object found in extraction response")
            return None

        try:
            parsed = json.loads(cleaned[start:end])
        except json.JSONDecodeError as exc:
            log.warning("ProjectMemory: JSON parse error: %s", exc)
            return None

        key_facts = parsed.get("key_facts", [])
        if not key_facts or not isinstance(key_facts, list):
            log.warning("ProjectMemory: key_facts missing or empty")
            return None

        return MemoryEntry(
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            run_id=run_id,
            task_summary=str(parsed.get("task_summary", ""))[:120],
            key_facts=[str(f) for f in key_facts[:_FACTS_PER_ENTRY]],
            stack=dict(stack),
            tags=[str(t) for t in parsed.get("tags", [])],
        )

    @classmethod
    def to_prompt_section(
        cls,
        memories: list[MemoryEntry],
        limit: int = _INJECT_LIMIT,
    ) -> str:
        """Render the most recent memories as a markdown section for task injection."""
        if not memories:
            return ""
        recent = memories[-limit:]
        lines = ["## Project Memory (From Past Runs)"]
        for m in recent:
            date = m.timestamp[:10] if m.timestamp else "?"
            lines.append(f"\n### {m.task_summary} ({date})")
            for fact in m.key_facts:
                lines.append(f"- {fact}")
        return "\n".join(lines)
