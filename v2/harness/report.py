from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from harness.status import StatusBlock, StatusCode

log = logging.getLogger(__name__)

MAX_PROGRESS_ENTRIES = 15

_STATUS_BLOCK_PATTERN = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_status_block(text: str) -> dict | None:
    """
    Extract the last JSON status block from agent output.
    Looks for ```json ... ``` blocks and returns the last one that has
    the expected run_id / status / agent fields.
    """
    matches = _STATUS_BLOCK_PATTERN.findall(text)
    for raw in reversed(matches):
        try:
            data = json.loads(raw)
            if {"run_id", "status", "agent"} <= data.keys():
                return data
        except json.JSONDecodeError:
            continue
    return None


def build_report_markdown(
    agent_name: str,
    task: str,
    provider_text: str,
    status_block: StatusBlock,
) -> str:
    title = agent_name.replace("_", " ").title()
    header = f"# {title} Report\n\n"
    task_section = f"## Task\n\n{task}\n\n"

    if provider_text:
        content_section = f"## Output\n\n{provider_text}\n\n"
    else:
        content_section = (
            "## Output\n\n"
            f"No output — agent run ended with status `{status_block.status.value}`.\n\n"
        )

    # Canonical status block always appended last so parse_status_block finds it.
    status_section = (
        "## Structured Status Block\n\n"
        "```json\n"
        + json.dumps(status_block.to_dict(), indent=2)
        + "\n```\n"
    )
    return header + task_section + content_section + status_section


def write_specs_report(
    content: str,
    specs_report_path: str,
    project_root: Path,
) -> Path | None:
    """
    Write (or overwrite) a persistent report under project_root/specs/.

    Called for agents with specs_report_path set (qa_engineer, code_reviewer,
    devops_engineer). Keeps the specs/ copies up-to-date across pipeline runs
    so later agents can read the latest QA/review/deploy results without
    knowing specific run IDs.

    Returns the path written, or None if specs_report_path is empty.
    """
    if not specs_report_path:
        return None
    full_path = project_root / specs_report_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    log.info("Specs report written: %s", full_path)
    return full_path


def write_run_report(
    run_id: str,
    agent_name: str,
    content: str,
    report_filename: str,
    runs_dir: Path,
) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / report_filename
    report_path.write_text(content, encoding="utf-8")
    log.info("Report written: %s", report_path)
    return report_path


def update_latest(
    run_id: str,
    task_id: str,
    agent_name: str,
    summary: str,
    status: StatusCode,
    files_changed: list[str],
    next_action: str,
    context_dir: Path,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    changed_list = "\n".join(f"- {f}" for f in files_changed) if files_changed else "None"
    content = (
        "# Latest Change\n\n"
        f"## Timestamp\n\n{now}\n\n"
        f"## Run ID\n\n{run_id}\n\n"
        f"## Task ID\n\n{task_id}\n\n"
        f"## Agent\n\n{agent_name}\n\n"
        f"## Summary\n\n{summary}\n\n"
        f"## Files Changed\n\n{changed_list}\n\n"
        f"## Result Status\n\n{status.value}\n\n"
        f"## Next Recommended Action\n\n{next_action}\n"
    )
    path = context_dir / "latest.md"
    path.write_text(content, encoding="utf-8")
    log.info("context/latest.md updated")


def update_progress(
    run_id: str,
    task_id: str,
    agent_name: str,
    summary: str,
    status: StatusCode,
    next_action: str,
    context_dir: Path,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    new_entry = (
        f"### {now} - {agent_name} - {status.value}\n\n"
        f"- Run ID: {run_id}\n"
        f"- Task ID: {task_id}\n"
        f"- Summary: {summary}\n"
        f"- Next Action: {next_action}\n"
    )

    path = context_dir / "progress.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    # Split header from entries
    header_end = existing.find("### ")
    if header_end == -1:
        header = existing or "# Progress\n\nNewest entry first. Keep latest 15 entries only.\n\n"
        raw_entries: list[str] = []
    else:
        header = existing[:header_end]
        entries_text = existing[header_end:]
        raw_entries = re.split(r"(?=### \d{4}-\d{2}-\d{2})", entries_text)
        raw_entries = [e for e in raw_entries if e.strip()]

    entries = [new_entry] + raw_entries
    entries = entries[:MAX_PROGRESS_ENTRIES]

    # Ensure the header ends with exactly one blank line before entries start.
    header_stripped = header.rstrip("\n")
    entries_block = "\n".join(e.rstrip() + "\n" for e in entries)
    path.write_text(
        header_stripped + "\n\n" + entries_block,
        encoding="utf-8",
    )
    log.info("context/progress.md updated (%d/%d entries)", len(entries), MAX_PROGRESS_ENTRIES)


def save_run_artifacts(
    run_id: str,
    agent_name: str,
    runs_dir: Path,
    agents_dir: Path,
    prompt_package: dict,
    provider_name: str,
    resolved_model: str,
    latency_s: float,
    usage: dict | None,
    provider_status: str,
    provider_error: str | None,
    tool_budget_summary: dict | None = None,
    tool_turns: int = 0,
    skipped_context_files: list[str] | None = None,
) -> None:
    """
    Save runtime inspectability artifacts under runs/<run_id>/:

      <agent>-meta.json              — provider/model/timing/usage metadata
      <agent>-assembled-prompt.md    — full assembled prompt with context manifest
      <agent>-spec.yaml              — copy of the agent YAML file
      <agent>-agent.md               — copy of the agent Markdown instructions
    """
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── meta.json ────────────────────────────────────────────────────────────
    meta = {
        "run_id": run_id,
        "agent": agent_name,
        "provider": provider_name,
        "model": resolved_model,
        "latency_seconds": round(latency_s, 3),
        "provider_status": provider_status,
        "provider_error": provider_error,
        "usage": usage or {},
        "tool_turns": tool_turns,
        "tool_budget": tool_budget_summary or {},
        "timestamp": now,
    }
    (run_dir / f"{agent_name}-meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    # ── assembled-prompt.md ───────────────────────────────────────────────────
    # Structured snapshot of the exact prompt assembled for this run.
    # Purpose: debug context ordering, truncation, stale content, assembly bugs.
    user_message: str = prompt_package.get("user_message", "")
    system_prompt: str = prompt_package.get("system_prompt", "")

    # Build context file manifest from the user_message (loaded files are embedded
    # by _build_prompt_package as "### <path>\n\n<content>").
    loaded_paths = _extract_context_paths(user_message)
    skipped = skipped_context_files or []

    manifest_lines: list[str] = []
    for p in loaded_paths:
        manifest_lines.append(f"  [+] {p}")
    for p in skipped:
        manifest_lines.append(f"  [-] {p}  (NOT FOUND — skipped)")
    manifest = "\n".join(manifest_lines) if manifest_lines else "  (none)"

    prompt_snap = (
        f"# Assembled Prompt — {agent_name}\n\n"
        f"| Field | Value |\n"
        f"|---|---|\n"
        f"| run_id | `{run_id}` |\n"
        f"| task_id | `{prompt_package.get('task_id', '')}` |\n"
        f"| agent | `{agent_name}` |\n"
        f"| model | `{resolved_model}` |\n"
        f"| provider | `{provider_name}` |\n"
        f"| tool_turns | `{tool_turns}` |\n"
        f"| assembled_at | `{now}` |\n\n"
        "## Context File Manifest\n\n"
        "```\n"
        f"{manifest}\n"
        "```\n\n"
        "---\n\n"
        "## System Prompt\n\n"
        f"{system_prompt}\n\n"
        "---\n\n"
        "## User Message\n\n"
        f"{user_message}\n"
    )
    (run_dir / f"{agent_name}-assembled-prompt.md").write_text(prompt_snap, encoding="utf-8")

    # ── spec.yaml + agent.md ──────────────────────────────────────────────────
    yaml_src = agents_dir / f"{agent_name}.yaml"
    md_src = agents_dir / f"{agent_name}.md"
    if yaml_src.exists():
        shutil.copy2(yaml_src, run_dir / f"{agent_name}-spec.yaml")
    if md_src.exists():
        shutil.copy2(md_src, run_dir / f"{agent_name}-agent.md")

    log.debug("Run artifacts saved for agent=%s run_id=%s", agent_name, run_id)


def _extract_context_paths(user_message: str) -> list[str]:
    """
    Extract context file paths from a user_message assembled by _build_prompt_package.

    Looks for '### <path>' headings inside the '## Context Files' section.
    """
    # Find the Context Files section
    ctx_start = user_message.find("## Context Files")
    if ctx_start == -1:
        return []
    ctx_section = user_message[ctx_start:]
    # Each file is introduced by "### <path>"
    return re.findall(r"^### (.+)$", ctx_section, re.MULTILINE)


def write_escalation_report(
    run_id: str,
    failed_agent: str,
    failure_chain: list[dict],
    last_error: str,
    runs_dir: Path,
) -> Path:
    """
    Generate runs/<run_id>/escalation-report.md when max_attempts is exceeded.

    failure_chain: list of completed step dicts from PipelineState.completed
                   [{step, agent, task_id, status}, ...]
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Summarise the failure chain
    chain_lines: list[str] = []
    for step in failure_chain:
        mark = "+" if step["status"] == "SUCCESS" else "!"
        chain_lines.append(
            f"  [{mark}] step {step['step']:>2}  {step['agent']:<22}  {step['status']}"
        )
    chain_text = "\n".join(chain_lines) if chain_lines else "  (no completed steps)"

    # Collect mutations from audit log (if present)
    mutations_path = runs_dir / run_id / "mutations.jsonl"
    mutation_lines: list[str] = []
    if mutations_path.exists():
        for raw in mutations_path.read_text(encoding="utf-8").strip().splitlines():
            try:
                entry = json.loads(raw)
                status_mark = "+" if entry.get("success") else "!"
                mutation_lines.append(
                    f"  [{status_mark}] {entry['tool']:<12} {entry['path']} "
                    f"({'ok' if entry.get('success') else entry.get('error','?')[:40]})"
                )
            except Exception:
                pass

    # Collect commands from audit log (if present)
    commands_path = runs_dir / run_id / "commands.jsonl"
    command_lines: list[str] = []
    if commands_path.exists():
        for raw in commands_path.read_text(encoding="utf-8").strip().splitlines():
            try:
                entry = json.loads(raw)
                exit_c = entry.get("exit_code")
                status_mark = "+" if entry.get("success") else "!"
                command_lines.append(
                    f"  [{status_mark}] {entry['command'][:60]}  exit={exit_c}"
                )
            except Exception:
                pass

    content = (
        "# Escalation Report\n\n"
        f"**Run ID:** {run_id}\n"
        f"**Generated:** {now}\n"
        f"**Failed Agent:** {failed_agent}\n\n"
        "---\n\n"
        "## Failure Chain\n\n"
        f"```\n{chain_text}\n```\n\n"
        "## Last Known Error\n\n"
        f"```\n{last_error}\n```\n\n"
        "## Files Modified This Run\n\n"
        + (
            "\n".join(mutation_lines) if mutation_lines
            else "_No file mutations recorded._"
        )
        + "\n\n"
        "## Commands Executed This Run\n\n"
        + (
            "\n".join(command_lines) if command_lines
            else "_No commands recorded._"
        )
        + "\n\n"
        "---\n\n"
        "## Recommended Human Action\n\n"
        f"The pipeline exceeded the maximum retry attempts for **{failed_agent}**.\n\n"
        "To investigate:\n"
        f"1. Review `runs/{run_id}/` for agent reports and logs.\n"
        f"2. Check `runs/{run_id}/mutations.jsonl` for file changes.\n"
        f"3. Check `runs/{run_id}/commands.jsonl` for command history.\n"
        "4. Fix the underlying issue manually.\n"
        f"5. Resume with: `python -m harness pipeline --resume {run_id}`\n"
    )

    report_path = runs_dir / run_id / "escalation-report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(content, encoding="utf-8")
    log.info("Escalation report written: %s", report_path)
    return report_path


_PROTECTED_SPECS: tuple[str, ...] = (
    "specs/prd.md",
    "specs/tech-design.md",
)


def is_protected_path(path: str | Path, project_root: Path) -> bool:
    """
    Returns True if path resolves to a protected spec file.

    Protected files (may be READ by agents but never PATCHED or overwritten):
      - specs/prd.md       — only the human user edits this
      - specs/tech-design.md — only system_architect may create it (via WRITE_FILE on new files);
                               no agent may PATCH or overwrite it once created
    """
    try:
        resolved = Path(path).resolve()
        for rel in _PROTECTED_SPECS:
            if resolved == (project_root / rel).resolve():
                return True
        return False
    except Exception:
        return False
