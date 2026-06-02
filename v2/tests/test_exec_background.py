"""Unit tests for EXEC_COMMAND background=true support."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from harness.tools.exec_cmd import exec_command
from harness.tools.mutations import MutationContext


def _make_ctx(tmp_path: Path) -> MutationContext:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    return MutationContext(
        run_id="test-run-1",
        agent_name="qa_engineer",
        task_id="task-1",
        runs_dir=runs_dir,
        project_root=tmp_path,
        hook_system=None,
    )


def test_background_start_returns_pid(tmp_path: Path) -> None:
    """background=true returns immediately with a PID, not stdout."""
    ctx = _make_ctx(tmp_path)
    result = exec_command(
        {"tool": "EXEC_COMMAND", "command": "python3 -m http.server 19876", "background": True},
        ctx,
    )
    assert result["status"] == "started"
    assert isinstance(result["pid"], int)
    assert result["pid"] > 0
    assert result["background"] is True

    # Clean up
    try:
        os.killpg(os.getpgid(result["pid"]), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def test_background_pid_tracked_in_ctx(tmp_path: Path) -> None:
    """PID is appended to ctx.background_pids for runner cleanup."""
    ctx = _make_ctx(tmp_path)
    assert ctx.background_pids == []

    result = exec_command(
        {"tool": "EXEC_COMMAND", "command": "python3 -m http.server 19877", "background": True},
        ctx,
    )
    assert result["pid"] in ctx.background_pids

    # Clean up
    try:
        os.killpg(os.getpgid(result["pid"]), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def test_background_server_actually_serves(tmp_path: Path) -> None:
    """Server started with background=true is reachable via curl."""
    ctx = _make_ctx(tmp_path)
    # Write a file so http.server has something to serve
    (tmp_path / "hello.txt").write_text("hello")

    result = exec_command(
        {
            "tool": "EXEC_COMMAND",
            "command": "python3 -m http.server 19878",
            "background": True,
            "cwd": str(tmp_path),
        },
        ctx,
    )
    pid = result["pid"]

    try:
        # Wait for server to be ready
        curl = exec_command(
            {
                "tool": "EXEC_COMMAND",
                "command": "curl --retry 10 --retry-delay 1 --retry-connrefused -s http://localhost:19878/hello.txt",
            },
            ctx,
        )
        assert curl["exit_code"] == 0
        assert "hello" in curl["stdout"]
    finally:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def test_background_pid_killed_by_runner(tmp_path: Path) -> None:
    """Simulate runner cleanup: all background_pids are killed after tool loop."""
    ctx = _make_ctx(tmp_path)

    result = exec_command(
        {"tool": "EXEC_COMMAND", "command": "python3 -m http.server 19879", "background": True},
        ctx,
    )
    pid = result["pid"]

    # Verify process is running
    assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0

    # Simulate what runner.py does after tool loop
    for p in ctx.background_pids:
        try:
            os.killpg(os.getpgid(p), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    ctx.background_pids.clear()

    time.sleep(0.3)  # give SIGTERM time to land

    # Process should be gone
    assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0
    assert ctx.background_pids == []
