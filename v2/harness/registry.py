from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Agents bundled with the harness package (one level up from harness/)
_BUNDLED_AGENTS_DIR = Path(__file__).parent.parent / "agents"


def resolve_agents_dir(project_root: Path) -> Path:
    """
    Return the agents directory to use.
    Priority:
      1. <project_root>/agents  — project-local override
      2. <harness_package>/agents — bundled default
    """
    local = project_root / "agents"
    if local.exists():
        return local
    return _BUNDLED_AGENTS_DIR


@dataclass
class AgentModelConfig:
    provider: str
    tier: str
    temperature: float = 0.2


@dataclass
class AgentSpec:
    name: str
    role_summary: str
    model: AgentModelConfig
    report_filename: str
    default_next_agent: str
    context_files: list[str]
    output_files: list[str]
    allowed_actions: list[str]
    forbidden_actions: list[str]
    instructions: str               # full contents of agents/<name>.md
    requires_human_gate_after: bool = False  # pipeline pauses after this agent
    max_attempts: int = 3           # max runs of this agent per pipeline run
    tool_budget: int = 80           # max tool calls per agent run
    # If non-empty, the run report is also written here under project_root
    # (e.g., "specs/qa-report.md"). Agents that produce persistent spec-level
    # reports use this to keep specs/ in sync across pipeline runs.
    specs_report_path: str = ""


def load_agent(name: str, agents_dir: Path) -> AgentSpec:
    yaml_path = agents_dir / f"{name}.yaml"
    md_path = agents_dir / f"{name}.md"

    if not yaml_path.exists():
        raise FileNotFoundError(f"Agent spec not found: {yaml_path}")
    if not md_path.exists():
        raise FileNotFoundError(f"Agent instructions not found: {md_path}")

    with yaml_path.open() as f:
        data = yaml.safe_load(f)

    raw_model = data.get("model", {})
    model = AgentModelConfig(
        provider=raw_model.get("provider", "openrouter"),
        tier=raw_model.get("tier", "strong"),
        temperature=float(raw_model.get("temperature", 0.2)),
    )

    return AgentSpec(
        name=data["name"],
        role_summary=data.get("role_summary", ""),
        model=model,
        report_filename=data.get("report_filename", f"{name}-report.md"),
        default_next_agent=data.get("default_next_agent", ""),
        context_files=data.get("context_files", []),
        output_files=data.get("output_files", []),
        allowed_actions=data.get("allowed_actions", []),
        forbidden_actions=data.get("forbidden_actions", []),
        instructions=md_path.read_text(),
        requires_human_gate_after=bool(data.get("requires_human_gate_after", False)),
        max_attempts=int(data.get("max_attempts", 3)),
        tool_budget=int(data.get("tool_budget", 80)),
        specs_report_path=data.get("specs_report_path", ""),
    )


def list_agents(agents_dir: Path) -> list[str]:
    return sorted(p.stem for p in agents_dir.glob("*.yaml"))
