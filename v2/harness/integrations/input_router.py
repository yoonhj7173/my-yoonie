from __future__ import annotations

"""
input_router — thread-local registry for overriding user input.

When harness runs inside the Slack bot, each pipeline thread registers
a Slack-aware input function here. conversation.py checks this registry
before falling back to stdin, so the pipeline never needs to know whether
it's running interactively or via Slack.

Usage:
    # In the pipeline thread (slack_bot.py):
    input_router.register(my_slack_input_fn)
    start_pipeline(...)          # conversation.py will call my_slack_input_fn
    input_router.unregister()

    # In conversation.py:
    fn = input_router.get()
    user_input = fn(summary) if fn else _read_terminal()
"""

import threading

_local = threading.local()


def register(fn) -> None:
    """Register a Slack input function for the current thread."""
    _local.fn = fn


def unregister() -> None:
    """Remove the registered input function for the current thread."""
    _local.fn = None


def get():
    """Return the registered input function, or None if using terminal."""
    return getattr(_local, "fn", None)
