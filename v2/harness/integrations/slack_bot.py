from __future__ import annotations

"""
Slack bot — remote control interface for harness.

Start with:  harness slack-listen

Supported DM commands:
  pipeline: <task>            start full pipeline from pm
  pipeline <agent>: <task>   start pipeline from a specific agent
  run <agent>: <task>        run a single agent
  status                     show recent pipelines
  help                       show this help

When a pipeline is paused for user input, just reply in the thread.
"""

import logging
import queue
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global: input queue + waiting flag
# One pipeline at a time; Slack replies route here when _is_waiting is set.
# ---------------------------------------------------------------------------

_input_queue: queue.Queue = queue.Queue()
_is_waiting = threading.Event()
_active_lock = threading.Lock()
_active: dict = {"run_id": None, "channel": None, "thread_ts": None}

# ---------------------------------------------------------------------------
# Input function registered in the pipeline thread via input_router
# ---------------------------------------------------------------------------

def _make_input_fn(web, channel: str, thread_ts: str):
    """Return a callable that notifies Slack and blocks until the user replies."""
    def fn(summary: str) -> str | None:
        msg = f"⏳ *Input needed*"
        if summary:
            msg += f"\n> {summary}"
        msg += "\n\nReply in this thread to continue."
        try:
            web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=msg)
        except Exception as exc:
            log.warning("Failed to post input-needed message: %s", exc)

        _is_waiting.set()
        try:
            return _input_queue.get(timeout=3600)
        except queue.Empty:
            return None
        finally:
            _is_waiting.clear()
            # Drain any stale messages
            while not _input_queue.empty():
                try:
                    _input_queue.get_nowait()
                except queue.Empty:
                    break

    return fn

# ---------------------------------------------------------------------------
# Pipeline / agent runners (called in background threads)
# ---------------------------------------------------------------------------

def _run_pipeline(start_agent: str, task: str, channel: str, thread_ts: str, web, project_root: Path) -> None:
    from harness.pipeline import start_pipeline  # noqa: PLC0415
    from harness.integrations import input_router  # noqa: PLC0415

    input_router.register(_make_input_fn(web, channel, thread_ts))
    try:
        state = start_pipeline(
            start_agent=start_agent,
            task=task,
            provider="openrouter",
            project_root=project_root,
            converse=True,
        )
        with _active_lock:
            _active.update(run_id=None, channel=None, thread_ts=None)

        mark = {
            "complete":  "✅", "failed": "❌", "paused": "⏸",
            "blocked": "🚧", "escalated": "🆘",
        }.get(state.status, "❓")
        steps = len(state.completed)
        _reply(web, channel, thread_ts,
               f"{mark} Pipeline *{state.status.upper()}* — {steps} step(s)\n`{state.run_id[-8:]}`")

    except Exception as exc:
        log.exception("Pipeline error")
        with _active_lock:
            _active.update(run_id=None, channel=None, thread_ts=None)
        _reply(web, channel, thread_ts, f"❌ Pipeline error: {exc}")
    finally:
        input_router.unregister()


def _run_agent(agent: str, task: str, channel: str, thread_ts: str, web, project_root: Path) -> None:
    from harness.runner import run_agent  # noqa: PLC0415

    try:
        status_block, _ = run_agent(
            agent_name=agent,
            task=task,
            provider_name="openrouter",
            project_root=project_root,
        )
        mark = "✅" if status_block.status.value == "SUCCESS" else "❌"
        summary = status_block.summary or ""
        _reply(web, channel, thread_ts,
               f"{mark} *{agent}* → `{status_block.status.value}`\n{summary[:300]}")
    except Exception as exc:
        log.exception("Agent run error")
        _reply(web, channel, thread_ts, f"❌ Error running {agent}: {exc}")

# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

_RE_PIPELINE = re.compile(r"^pipeline(?:\s+(\w+))?:\s*(.+)", re.IGNORECASE | re.DOTALL)
_RE_RUN      = re.compile(r"^run\s+(\w+):\s*(.+)",           re.IGNORECASE | re.DOTALL)
_RE_STATUS   = re.compile(r"^status$",                        re.IGNORECASE)
_RE_HELP     = re.compile(r"^help$",                          re.IGNORECASE)

_HELP = """\
*Harness Bot* — commands:

`pipeline: <task>` — full pipeline starting from pm
`pipeline <agent>: <task>` — start from a specific agent
  agents: pm · arch · swe · qa · cr · devops
`run <agent>: <task>` — single agent run
`status` — recent pipelines
`help` — this message

When a pipeline is waiting for your input, just reply in the thread."""


def _dispatch(text: str, channel: str, thread_ts: str, web, project_root: Path) -> None:
    from harness.state import resolve_agent, is_known_agent  # noqa: PLC0415

    # Route to waiting pipeline first
    if _is_waiting.is_set():
        _input_queue.put(text)
        _reply(web, channel, thread_ts, "✅ Got it — resuming...")
        return

    m = _RE_PIPELINE.match(text)
    if m:
        with _active_lock:
            if _active["run_id"] is not None:
                _reply(web, channel, thread_ts, "⚠️ A pipeline is already running.")
                return
            _active.update(channel=channel, thread_ts=thread_ts, run_id="pending")

        alias   = m.group(1) or "pm"
        task    = m.group(2).strip()
        agent   = resolve_agent(alias)
        if not is_known_agent(agent):
            with _active_lock:
                _active.update(run_id=None)
            _reply(web, channel, thread_ts, f"❌ Unknown agent: `{alias}`")
            return

        _reply(web, channel, thread_ts,
               f"🚀 Starting pipeline from *{agent}*\n> {task[:120]}")
        threading.Thread(
            target=_run_pipeline,
            args=(agent, task, channel, thread_ts, web, project_root),
            daemon=True,
        ).start()
        return

    m = _RE_RUN.match(text)
    if m:
        alias = m.group(1)
        task  = m.group(2).strip()
        agent = resolve_agent(alias)
        if not is_known_agent(agent):
            _reply(web, channel, thread_ts, f"❌ Unknown agent: `{alias}`")
            return
        _reply(web, channel, thread_ts, f"▶️ Running *{agent}*\n> {task[:120]}")
        threading.Thread(
            target=_run_agent,
            args=(agent, task, channel, thread_ts, web, project_root),
            daemon=True,
        ).start()
        return

    if _RE_STATUS.match(text):
        _cmd_status(web, channel, thread_ts, project_root)
        return

    if _RE_HELP.match(text):
        _reply(web, channel, thread_ts, _HELP)
        return

    _reply(web, channel, thread_ts, f"❓ Unknown command. Type `help` for available commands.")


def _cmd_status(web, channel: str, thread_ts: str, project_root: Path) -> None:
    from harness.pipeline import list_pipelines  # noqa: PLC0415

    pipelines = list_pipelines(project_root / "runs")[:5]
    if not pipelines:
        _reply(web, channel, thread_ts, "No pipelines found yet.")
        return

    marks = {"complete": "✅", "failed": "❌", "paused": "⏸", "running": "🔄", "blocked": "🚧", "escalated": "🆘"}
    lines = ["*Recent pipelines:*"]
    for p in pipelines:
        mark = marks.get(p.status, "❓")
        ts   = (p.created_at or "")[:16].replace("T", " ")
        lines.append(f"{mark} `{p.run_id[-8:]}` {p.status:<10} {ts}")
    _reply(web, channel, thread_ts, "\n".join(lines))


def _save_notify_channel(channel: str) -> None:
    import json as _json  # noqa: PLC0415
    config_path = Path.home() / ".claude" / "slack_config.json"
    try:
        config = _json.loads(config_path.read_text()) if config_path.exists() else {}
        if config.get("channel_id") != channel:
            config["channel_id"] = channel
            config_path.write_text(_json.dumps(config, indent=2))
    except Exception:
        pass


def _reply(web, channel: str, thread_ts: str, text: str) -> None:
    try:
        web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
    except Exception as exc:
        log.warning("Slack reply failed: %s", exc)

# ---------------------------------------------------------------------------
# Bot entry point
# ---------------------------------------------------------------------------

def run_bot(project_root: Path) -> None:
    """Connect to Slack via Socket Mode and block until interrupted."""
    import os  # noqa: PLC0415

    try:
        from slack_sdk.socket_mode import SocketModeClient          # noqa: PLC0415
        from slack_sdk.web import WebClient                         # noqa: PLC0415
        from slack_sdk.socket_mode.response import SocketModeResponse  # noqa: PLC0415
    except ImportError:
        raise RuntimeError(
            "slack_sdk is not installed.\n"
            "Run: pip install slack-sdk"
        )

    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not app_token:
        raise RuntimeError(
            "SLACK_APP_TOKEN is not set.\n"
            "Get it from api.slack.com/apps → Settings → Basic Information → App-Level Tokens.\n"
            "Then: export SLACK_APP_TOKEN=xapp-..."
        )
    if not bot_token:
        raise RuntimeError(
            "SLACK_BOT_TOKEN is not set.\n"
            "export SLACK_BOT_TOKEN=xoxb-..."
        )

    web    = WebClient(token=bot_token)
    bot_id = web.auth_test()["user_id"]
    client = SocketModeClient(app_token=app_token, web_client=web)

    def _handle(client: SocketModeClient, req) -> None:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        if req.type != "events_api":
            return

        event = req.payload.get("event", {})
        if event.get("type") != "message" or event.get("subtype"):
            return
        if event.get("user") == bot_id:
            return

        text      = (event.get("text") or "").strip()
        channel   = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        if not text:
            return

        log.info("Slack message  channel=%s  text=%r", channel, text[:80])
        _save_notify_channel(channel)
        _dispatch(text, channel, thread_ts, web, project_root)

    client.socket_mode_request_listeners.append(_handle)
    client.connect()

    print("Harness bot connected. Listening for Slack messages…", flush=True)
    print("Send a DM to the bot or mention it in a channel.", flush=True)
    print("Type Ctrl+C to stop.\n", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping bot…")
        client.close()
