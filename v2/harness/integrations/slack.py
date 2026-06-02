from __future__ import annotations

"""
Slack P0 — outbound-only notifications via Incoming Webhook or Bot Token.

Two modes (auto-detected by env var availability):
  Bot Token (SLACK_BOT_TOKEN)    → Slack Web API, threading supported
  Webhook   (SLACK_WEBHOOK_URL)  → Incoming Webhook, no threading

Rules:
  - Slack credentials come from environment variables ONLY.
  - Webhook URL / Bot Token are NEVER logged, printed, or stored in files.
  - Slack errors never propagate — pipeline continues regardless.
  - Every send attempt is logged to runs/<run_id>/slack.jsonl.

P0 scope: outbound notifications only.
P1 (not here): inbound replies, approval buttons, slash commands, OAuth.

Bot Token requires Slack app permission: chat:write
(OAuth & Permissions → Bot Token Scopes → chat:write, chat:write.public)
"""

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.hooks import (
    AFTER_AGENT_RUN,
    HookSystem,
    ON_COMMAND_FAILURE,
    ON_ESCALATION,
    ON_HUMAN_APPROVAL_REQUIRED,
    ON_WORKFLOW_COMPLETE,
    ON_WORKFLOW_PAUSED,
    ON_WORKFLOW_RESUMED,
)

log = logging.getLogger(__name__)

_SLACK_API_ENDPOINT = "https://slack.com/api/chat.postMessage"
_SLACK_LOG_FILENAME = "slack.jsonl"
_HTTP_TIMEOUT_S = 10

# ── Agent display metadata ──────────────────────────────────────────────────

_AGENT_EMOJI: dict[str, str] = {
    "product_manager":   "🤖",
    "system_architect":  "🏗️",
    "software_engineer": "👨‍💻",
    "qa_engineer":       "🧪",
    "code_reviewer":     "🔍",
    "devops_engineer":   "🚀",
}

_AGENT_LABEL: dict[str, str] = {
    "product_manager":   "Product Manager",
    "system_architect":  "System Architect",
    "software_engineer": "Software Engineer",
    "qa_engineer":       "QA Engineer",
    "code_reviewer":     "Code Reviewer",
    "devops_engineer":   "DevOps Engineer",
}

_STATUS_EMOJI: dict[str, str] = {
    "SUCCESS":          "✅",
    "FAILED":           "❌",
    "BLOCKED":          "🚫",
    "NEEDS_USER_INPUT": "⚠️",
    "SKIPPED":          "⏭️",
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class SlackConfig:
    enabled: bool
    webhook_url: str       # from env — never stored in files
    bot_token: str         # from env — never stored in files
    control_channel: str
    project_channel: str
    notify_control_channel: bool
    project_name: str

    @property
    def has_bot_token(self) -> bool:
        return bool(self.bot_token)

    @property
    def has_webhook(self) -> bool:
        return bool(self.webhook_url)

    @property
    def effective_project_channel(self) -> str:
        """Project channel, falling back to control channel if unset."""
        return self.project_channel or self.control_channel

    @property
    def can_send(self) -> bool:
        return (
            self.enabled
            and (self.has_bot_token or self.has_webhook)
            and bool(self.effective_project_channel)
        )


def load_slack_config(project_root: Path) -> SlackConfig | None:
    """
    Load Slack config from config/harness.yaml.

    Returns None if the file is missing or has no [slack] section.
    Credentials are read from environment variables only.
    """
    config_path = project_root / "config" / "harness.yaml"
    if not config_path.exists():
        return None

    try:
        import yaml  # noqa: PLC0415
        with config_path.open() as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as exc:
        log.warning("Could not read harness.yaml for Slack config: %s", exc)
        return None

    slack_raw = cfg.get("slack")
    if not slack_raw:
        return None

    enabled = bool(slack_raw.get("enabled", False))

    webhook_url_env = slack_raw.get("webhook_url_env", "SLACK_WEBHOOK_URL")
    bot_token_env   = slack_raw.get("bot_token_env",   "SLACK_BOT_TOKEN")

    # Credentials from env only — log var names, never values
    webhook_url = os.environ.get(webhook_url_env, "")
    bot_token   = os.environ.get(bot_token_env,   "")

    if enabled and not webhook_url and not bot_token:
        log.warning(
            "Slack is enabled but neither %s nor %s env var is set. "
            "Slack notifications will be disabled for this run.",
            webhook_url_env, bot_token_env,
        )

    control_channel = slack_raw.get("control_channel", "")
    project_channel = slack_raw.get("project_channel", "") or control_channel

    if enabled and not project_channel:
        log.warning(
            "Slack is enabled but no project_channel or control_channel configured. "
            "Slack notifications will be disabled for this run."
        )

    project_name = cfg.get("project", {}).get("name", "")

    return SlackConfig(
        enabled=enabled,
        webhook_url=webhook_url,
        bot_token=bot_token,
        control_channel=control_channel,
        project_channel=project_channel,
        notify_control_channel=bool(slack_raw.get("notify_control_channel", True)),
        project_name=project_name,
    )


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

class SlackNotifier:
    """
    Outbound-only Slack notifier.

    If bot_token is available → uses Web API (chat.postMessage) → threading works.
    If only webhook_url is available → uses Incoming Webhook → no threading.

    All errors are caught and logged; never propagated to caller.
    """

    def __init__(self, config: SlackConfig) -> None:
        self.cfg = config

    # ── High-level send helpers ───────────────────────────────────────────────

    def send(
        self,
        text: str,
        channel: str,
        *,
        thread_ts: str = "",
        run_id: str = "",
        runs_dir: Path | None = None,
        event: str = "",
    ) -> str | None:
        """
        Send a message. Returns message ts (for threading) if available, else None.
        Never raises.
        """
        if not self.cfg.can_send:
            return None

        ts: str | None = None
        error: str | None = None

        try:
            if self.cfg.has_bot_token:
                ts = self._post_api(channel=channel, text=text, thread_ts=thread_ts)
            else:
                self._post_webhook(text=text)
        except Exception as exc:
            # Log the error without exposing credentials
            error = type(exc).__name__ + ": " + str(exc)
            log.warning("Slack send failed (event=%r channel=%r): %s", event, channel, error)

        if runs_dir and run_id:
            self._write_log(
                runs_dir=runs_dir, run_id=run_id,
                event=event, channel=channel,
                thread_ts=thread_ts or ts or "",
                success=(error is None), error=error,
            )

        return ts

    def send_to_project(
        self,
        text: str,
        *,
        thread_ts: str = "",
        run_id: str = "",
        runs_dir: Path | None = None,
        event: str = "",
    ) -> str | None:
        return self.send(
            text, self.cfg.effective_project_channel,
            thread_ts=thread_ts, run_id=run_id, runs_dir=runs_dir, event=event,
        )

    def send_to_control(
        self,
        text: str,
        *,
        run_id: str = "",
        runs_dir: Path | None = None,
        event: str = "",
    ) -> None:
        """Send to control channel (no threading — it's a different channel)."""
        if not self.cfg.notify_control_channel or not self.cfg.control_channel:
            return
        self.send(
            text, self.cfg.control_channel,
            run_id=run_id, runs_dir=runs_dir, event=event,
        )

    # ── Pipeline lifecycle ────────────────────────────────────────────────────

    def send_pipeline_started(
        self, run_id: str, start_agent: str, task: str, runs_dir: Path,
    ) -> str | None:
        """
        Send the pipeline-start parent message to project_channel.
        Returns thread_ts (bot token mode) or None (webhook mode).
        """
        text = self._fmt_pipeline_started(run_id, start_agent, task)
        ts = self.send_to_project(text, run_id=run_id, runs_dir=runs_dir, event="pipeline_started")

        if self.cfg.has_bot_token and not ts:
            log.debug("Slack: bot token mode but no ts returned — threading unavailable this run")
        elif not self.cfg.has_bot_token:
            log.debug("Slack: webhook mode — threading not supported (set SLACK_BOT_TOKEN to enable)")

        # Brief notice to control channel
        proj = self.cfg.project_name or "?"
        ctrl_text = f"🚀 Pipeline started | *{proj}* | `{run_id}` | start: `{start_agent}`"
        self.send_to_control(ctrl_text, run_id=run_id, runs_dir=runs_dir, event="pipeline_started")

        return ts

    # ── Hook registration ─────────────────────────────────────────────────────

    def register_hooks(
        self,
        hook_system: HookSystem,
        state: Any,        # PipelineState — duck-typed to avoid circular import
        runs_dir: Path,
    ) -> None:
        """
        Register all P0 Slack hook handlers.
        Closes over `state` so thread_ts is always current at call time.
        """
        def _ts() -> str:
            return getattr(state, "slack_thread_ts", "")

        def _kw(event: str) -> dict:
            return dict(thread_ts=_ts(), run_id=state.run_id, runs_dir=runs_dir, event=event)

        def _ckw(event: str) -> dict:  # control channel kwargs (no thread_ts)
            return dict(run_id=state.run_id, runs_dir=runs_dir, event=event + "_ctrl")

        def on_agent_run(p: dict) -> None:
            self.send_to_project(self._fmt_agent_run(p), **_kw(AFTER_AGENT_RUN))

        def on_complete(p: dict) -> None:
            msg = self._fmt_complete(p)
            self.send_to_project(msg, **_kw(ON_WORKFLOW_COMPLETE))
            self.send_to_control(msg, **_ckw(ON_WORKFLOW_COMPLETE))

        def on_paused(p: dict) -> None:
            msg = self._fmt_paused(p)
            self.send_to_project(msg, **_kw(ON_WORKFLOW_PAUSED))
            self.send_to_control(msg, **_ckw(ON_WORKFLOW_PAUSED))

        def on_resumed(p: dict) -> None:
            self.send_to_project(self._fmt_resumed(p), **_kw(ON_WORKFLOW_RESUMED))

        def on_approval(p: dict) -> None:
            msg = self._fmt_approval(p)
            self.send_to_project(msg, **_kw(ON_HUMAN_APPROVAL_REQUIRED))
            self.send_to_control(msg, **_ckw(ON_HUMAN_APPROVAL_REQUIRED))

        def on_escalation(p: dict) -> None:
            msg = self._fmt_escalation(p)
            self.send_to_project(msg, **_kw(ON_ESCALATION))
            self.send_to_control(msg, **_ckw(ON_ESCALATION))

        def on_cmd_failure(p: dict) -> None:
            self.send_to_project(self._fmt_cmd_failure(p), **_kw(ON_COMMAND_FAILURE))

        hook_system.on(AFTER_AGENT_RUN,            on_agent_run)
        hook_system.on(ON_WORKFLOW_COMPLETE,       on_complete)
        hook_system.on(ON_WORKFLOW_PAUSED,         on_paused)
        hook_system.on(ON_WORKFLOW_RESUMED,        on_resumed)
        hook_system.on(ON_HUMAN_APPROVAL_REQUIRED, on_approval)
        hook_system.on(ON_ESCALATION,              on_escalation)
        hook_system.on(ON_COMMAND_FAILURE,         on_cmd_failure)

    # ── HTTP layer ────────────────────────────────────────────────────────────

    def _post_api(self, channel: str, text: str, thread_ts: str = "") -> str | None:
        """Post via Slack Web API. Returns message ts for threading."""
        payload: dict = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _SLACK_API_ENDPOINT,
            data=data,
            headers={
                "Content-Type": "application/json",
                # Bot Token auth — value never logged
                "Authorization": f"Bearer {self.cfg.bot_token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        if not body.get("ok"):
            raise RuntimeError(f"Slack API error: {body.get('error', 'unknown')}")

        return body.get("ts")

    def _post_webhook(self, text: str) -> None:
        """Post via Incoming Webhook. No thread_ts returned."""
        data = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            self.cfg.webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8")
        if body.strip() != "ok":
            raise RuntimeError(f"Slack webhook unexpected response: {body[:100]!r}")

    # ── Audit log ─────────────────────────────────────────────────────────────

    def _write_log(
        self,
        *,
        runs_dir: Path,
        run_id: str,
        event: str,
        channel: str,
        thread_ts: str,
        success: bool,
        error: str | None,
    ) -> None:
        try:
            entry = {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event": event,
                "channel": channel,
                "thread_ts": thread_ts,
                "success": success,
                "error": error,
            }
            log_path = runs_dir / run_id / _SLACK_LOG_FILENAME
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.debug("Could not write slack.jsonl: %s", exc)

    # ── Message formatters ────────────────────────────────────────────────────

    def _proj_run(self, run_id: str) -> str:
        parts = []
        if self.cfg.project_name:
            parts.append(f"*Project:* {self.cfg.project_name}")
        parts.append(f"*Run:* `{run_id}`")
        return "  |  ".join(parts)

    def _fmt_pipeline_started(self, run_id: str, start_agent: str, task: str) -> str:
        task_preview = task[:120] + ("…" if len(task) > 120 else "")
        return "\n".join([
            "🚀 *Pipeline Started*", "",
            self._proj_run(run_id),
            f"*Start Agent:* `{start_agent}`",
            f"*Task:* {task_preview}",
        ])

    def _fmt_agent_run(self, p: dict) -> str:
        agent     = p.get("agent", "unknown")
        status    = p.get("status", "?")
        summary   = p.get("summary", "")
        run_id    = p.get("run_id", "")
        next_a    = p.get("next_agent", "")
        report    = p.get("report_path", "")

        emoji  = _AGENT_EMOJI.get(agent, "🤖")
        label  = _AGENT_LABEL.get(agent, agent)
        st_ico = _STATUS_EMOJI.get(status, "")

        lines = [f"{emoji} *[{label}]*", "", self._proj_run(run_id),
                 f"*Status:* {st_ico} {status}"]
        if summary:
            lines.append(f"*Summary:* {summary[:300]}")
        if next_a:
            lines.append(f"*Next:* `{next_a}`")
        if report:
            lines.append(f"*Report:* `{report}`")
        return "\n".join(lines)

    def _fmt_complete(self, p: dict) -> str:
        run_id = p.get("run_id", "")
        steps  = p.get("completed_steps", 0)
        return "\n".join([
            "✅ *Pipeline Complete*", "",
            self._proj_run(run_id),
            f"*Completed steps:* {steps}",
        ])

    def _fmt_paused(self, p: dict) -> str:
        run_id   = p.get("run_id", "")
        agent    = p.get("agent", "")
        reason   = p.get("reason", "")
        next_a   = p.get("next_agent", "")
        substate = p.get("pause_substate", "")

        header = {
            "waiting_for_human": "⏸️ *Waiting for Human Review*",
            "needs_user_input":  "⚠️ *Needs User Input*",
        }.get(substate, "⏸️ *Pipeline Paused*")

        lines = [header, "", self._proj_run(run_id)]
        if agent:
            lines.append(f"*Paused after:* `{agent}`")
        if reason:
            lines.append(f"*Reason:* {reason}")
        if next_a:
            lines.append(f"*Next Agent:* `{next_a}`")
        lines.append(f"\n_Resume:_ `python -m harness pipeline --resume {run_id}`")
        return "\n".join(lines)

    def _fmt_resumed(self, p: dict) -> str:
        run_id  = p.get("run_id", "")
        current = p.get("current_agent", "")
        lines   = ["▶️ *Pipeline Resumed*", "", self._proj_run(run_id)]
        if current:
            lines.append(f"*Resuming at:* `{current}`")
        return "\n".join(lines)

    def _fmt_approval(self, p: dict) -> str:
        run_id = p.get("run_id", "")
        agent  = p.get("agent", "")
        reason = p.get("reason", "")
        next_a = p.get("next_agent", "")

        lines = ["⚠️ *Human Input Required*", "", self._proj_run(run_id)]
        if agent:
            lines.append(f"*Agent:* `{agent}`")
        if reason:
            lines.append(f"*Reason:* {reason}")
        if next_a:
            lines.append(f"*Next Agent when ready:* `{next_a}`")
        lines.append(f"\n_Resume:_ `python -m harness pipeline --resume {run_id}`")
        return "\n".join(lines)

    def _fmt_escalation(self, p: dict) -> str:
        run_id = p.get("run_id", "")
        agent  = p.get("agent", "")
        reason = p.get("reason", "")
        report = p.get("escalation_report", "")

        lines = ["🚨 *Escalation — Max Attempts Exceeded*", "", self._proj_run(run_id)]
        if agent:
            lines.append(f"*Failed Agent:* `{agent}`")
        if reason:
            lines.append(f"*Reason:* {reason}")
        if report:
            lines.append(f"*Escalation Report:* `{report}`")
        return "\n".join(lines)

    def _fmt_cmd_failure(self, p: dict) -> str:
        run_id    = p.get("run_id", "")
        agent     = p.get("agent", "")
        command   = p.get("command", "")
        exit_code = p.get("exit_code")
        stderr    = (p.get("stderr") or "").strip()

        emoji = _AGENT_EMOJI.get(agent, "🤖")
        label = _AGENT_LABEL.get(agent, agent)

        lines = ["💥 *Command Failed*", "", self._proj_run(run_id),
                 f"*Agent:* {emoji} {label}"]
        if command:
            lines.append(f"*Command:* `{command[:80]}`")
        if exit_code is not None:
            lines.append(f"*Exit Code:* `{exit_code}`")
        if stderr:
            snippet = stderr[:200]
            lines.append(f"*stderr:*\n```{snippet}```")
        return "\n".join(lines)
