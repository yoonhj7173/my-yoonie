"""
E2E pipeline routing tests.

Tests three previously-unverified scenarios:
  1. Debugger handoff   — code_reviewer BLOCKED → debugger_engineer → code_reviewer resume
  2. Conversation mode  — NEEDS_USER_INPUT enters interactive loop → SUCCESS → continues
  3. Playwright tools   — browser.py imports and can open a real browser page

All tests use mock run_agent so they don't need a real LLM key.
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.hooks import HookSystem
from harness.pipeline import PipelineState, _execute
from harness.status import StatusBlock, StatusCode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent   # harness/v2/


def _block(agent: str, status: str, *, summary: str = "", issues: list[str] | None = None) -> StatusBlock:
    return StatusBlock(
        run_id="test-run-e2e",
        task_id=f"task-{agent}",
        agent=agent,
        status=StatusCode[status],
        summary=summary or f"{agent} {status}",
        issues_found=issues or [],
    )


def _state(start: str, run_id: str = "test-run-e2e") -> PipelineState:
    return PipelineState(
        run_id=run_id,
        task="e2e test task",
        start_agent=start,
        provider="stub",
        status="running",
        current_agent=start,
    )


def _noop_hooks() -> HookSystem:
    hs = HookSystem()
    return hs


# ---------------------------------------------------------------------------
# Test 1 — Debugger handoff routing
# ---------------------------------------------------------------------------

class TestDebuggerHandoff:
    """
    code_reviewer returns BLOCKED
      → pipeline routes to debugger_engineer
      → debugger_engineer returns SUCCESS
      → pipeline routes back to code_reviewer
      → code_reviewer returns SUCCESS
      → pipeline routes to devops_engineer
      → devops_engineer returns SUCCESS (no human gate)
      → pipeline complete
    """

    def test_blocked_routes_to_debugger_then_resumes(self, tmp_path):
        state = _state("code_reviewer")
        state.debugger_from = ""   # not yet set

        call_sequence = [
            ("code_reviewer",      "BLOCKED"),   # 1st call: trigger debugger
            ("debugger_engineer",  "SUCCESS"),   # 2nd call: debugger fixed it
            ("code_reviewer",      "SUCCESS"),   # 3rd call: reviewer passes
            ("devops_engineer",    "SUCCESS"),   # 4th call: devops done
        ]
        call_iter = iter(call_sequence)

        def fake_run_agent(agent_name, task, provider_name, project_root, run_id, hook_system, **kw):
            expected_agent, status = next(call_iter)
            assert agent_name == expected_agent, (
                f"Expected run_agent({expected_agent!r}) but got ({agent_name!r})"
            )
            return _block(agent_name, status), f"## response from {agent_name}"

        with patch("harness.pipeline.run_agent", side_effect=fake_run_agent), \
             patch("harness.pipeline._save"):   # skip disk writes
            final = _execute(state, PROJECT_ROOT, _noop_hooks(), converse=False)

        assert final.status == "complete", f"Expected complete, got {final.status}"
        agents_run = [c["agent"] for c in final.completed]
        assert agents_run == ["code_reviewer", "debugger_engineer", "code_reviewer", "devops_engineer"], \
            f"Unexpected agent sequence: {agents_run}"

    def test_debugger_from_is_set_correctly(self, tmp_path):
        """PipelineState.debugger_from must record who triggered the debugger."""
        state = _state("code_reviewer")

        captured_debugger_from: list[str] = []

        def fake_run_agent(agent_name, task, provider_name, project_root, run_id, hook_system, **kw):
            if agent_name == "debugger_engineer":
                captured_debugger_from.append(state.debugger_from)
                return _block("debugger_engineer", "SUCCESS"), ""
            if agent_name == "code_reviewer":
                if not captured_debugger_from:
                    return _block("code_reviewer", "BLOCKED"), ""
                return _block("code_reviewer", "SUCCESS"), ""
            return _block(agent_name, "SUCCESS"), ""

        with patch("harness.pipeline.run_agent", side_effect=fake_run_agent), \
             patch("harness.pipeline._save"):
            _execute(state, PROJECT_ROOT, _noop_hooks(), converse=False)

        assert captured_debugger_from == ["code_reviewer"], \
            f"debugger_from should be 'code_reviewer', got {captured_debugger_from}"

    def test_qa_blocked_also_routes_to_debugger(self, tmp_path):
        """qa_engineer BLOCKED should also hand off to debugger_engineer."""
        state = _state("qa_engineer")

        call_sequence = [
            ("qa_engineer",        "BLOCKED"),
            ("debugger_engineer",  "SUCCESS"),
            ("qa_engineer",        "SUCCESS"),
            ("code_reviewer",      "SUCCESS"),
            ("devops_engineer",    "SUCCESS"),
        ]
        call_iter = iter(call_sequence)

        def fake_run_agent(agent_name, *a, **kw):
            expected, status = next(call_iter)
            assert agent_name == expected, f"Got {agent_name!r}, expected {expected!r}"
            return _block(agent_name, status), ""

        with patch("harness.pipeline.run_agent", side_effect=fake_run_agent), \
             patch("harness.pipeline._save"):
            final = _execute(state, PROJECT_ROOT, _noop_hooks(), converse=False)

        assert final.status == "complete"
        agents_run = [c["agent"] for c in final.completed]
        assert agents_run == ["qa_engineer", "debugger_engineer", "qa_engineer", "code_reviewer", "devops_engineer"]


# ---------------------------------------------------------------------------
# Test 2 — Conversation mode (NEEDS_USER_INPUT → loop → SUCCESS)
# ---------------------------------------------------------------------------

class TestConversationMode:
    """
    Agent returns NEEDS_USER_INPUT
      → enter_conversation_loop() is called
      → user types a response
      → agent re-runs and returns SUCCESS
      → pipeline continues to next agent
    """

    def test_needs_user_input_enters_conversation_loop(self, tmp_path):
        state = _state("software_engineer")

        final_block_from_loop = _block("software_engineer", "SUCCESS",
                                        summary="Fixed after user clarification")
        mock_conversation_loop = MagicMock(return_value=(final_block_from_loop, []))

        def fake_run_agent(agent_name, *a, **kw):
            if agent_name == "software_engineer":
                return _block("software_engineer", "NEEDS_USER_INPUT",
                               summary="Need clarification on auth"), ""
            return _block(agent_name, "SUCCESS"), ""

        with patch("harness.pipeline.run_agent", side_effect=fake_run_agent), \
             patch("harness.pipeline.enter_conversation_loop", mock_conversation_loop), \
             patch("harness.pipeline._save"):
            final = _execute(state, PROJECT_ROOT, _noop_hooks(), converse=True)

        # Conversation loop must have been called exactly once for software_engineer
        mock_conversation_loop.assert_called_once()
        call_kwargs = mock_conversation_loop.call_args
        assert call_kwargs.kwargs.get("agent_name") == "software_engineer" or \
               call_kwargs.args[0] == "software_engineer", \
               f"enter_conversation_loop called with wrong agent: {call_kwargs}"

    def test_no_converse_pauses_instead_of_looping(self, tmp_path):
        state = _state("software_engineer")

        mock_conversation_loop = MagicMock()

        def fake_run_agent(agent_name, *a, **kw):
            return _block("software_engineer", "NEEDS_USER_INPUT"), ""

        with patch("harness.pipeline.run_agent", side_effect=fake_run_agent), \
             patch("harness.pipeline.enter_conversation_loop", mock_conversation_loop), \
             patch("harness.pipeline._save"):
            final = _execute(state, PROJECT_ROOT, _noop_hooks(), converse=False)

        # With converse=False, conversation loop must NOT be entered
        mock_conversation_loop.assert_not_called()
        assert final.status == "paused"
        assert final.pause_substate == "needs_user_input"

    def test_conversation_loop_real_flow(self, tmp_path, capsys):
        """
        Tests the actual enter_conversation_loop() function with mocked stdin.
        Agent returns NEEDS_USER_INPUT once, user types response, agent returns SUCCESS.
        """
        from harness.conversation import enter_conversation_loop

        run_sequence = [
            _block("software_engineer", "NEEDS_USER_INPUT",
                   summary="What auth method should I use?"),
            _block("software_engineer", "SUCCESS",
                   summary="Used JWT as instructed by user"),
        ]
        seq_iter = iter(run_sequence)

        def fake_run_agent(agent_name, task, *a, **kw):
            blk = next(seq_iter)
            return blk, f"Agent response: {blk.summary}"

        # Feed "use JWT" as user input, then the loop exits on SUCCESS
        with patch("harness.runner.run_agent", side_effect=fake_run_agent), \
             patch("harness.conversation.run_agent", side_effect=fake_run_agent), \
             patch("builtins.input", return_value="use JWT please"):
            result, _ = enter_conversation_loop(
                agent_name="software_engineer",
                task="Build auth system",
                provider_name="stub",
                project_root=PROJECT_ROOT,
                run_id="test-run-e2e",
                initial_summary="Need user input",
                hook_system=_noop_hooks(),
            )

        assert result.status == StatusCode.SUCCESS
        assert "JWT" in result.summary

    def test_conversation_user_exit_returns_blocked(self, tmp_path):
        """If user types 'exit', conversation returns BLOCKED."""
        from harness.conversation import enter_conversation_loop

        def fake_run_agent(agent_name, task, *a, **kw):
            return _block("software_engineer", "NEEDS_USER_INPUT"), "response"

        with patch("harness.conversation.run_agent", side_effect=fake_run_agent), \
             patch("builtins.input", return_value="exit"):
            result, _ = enter_conversation_loop(
                agent_name="software_engineer",
                task="task",
                provider_name="stub",
                project_root=PROJECT_ROOT,
                run_id="test-run-e2e",
                hook_system=_noop_hooks(),
            )

        assert result.status == StatusCode.BLOCKED


# ---------------------------------------------------------------------------
# Test 3 — Playwright browser tools
# ---------------------------------------------------------------------------

class TestPlaywrightTools:
    """
    Verifies the browser.py module is importable and that the tool dispatcher
    recognises BROWSER_* tool names.
    """

    def test_browser_module_importable(self):
        """browser.py must import without error."""
        from harness.tools import browser   # noqa: F401

    def test_browser_functions_exist(self):
        from harness.tools.browser import (
            browser_navigate,
            browser_screenshot,
            browser_click,
            browser_fill,
            browser_eval,
            browser_get_text,
        )
        for fn in [browser_navigate, browser_screenshot, browser_click,
                   browser_fill, browser_eval, browser_get_text]:
            assert callable(fn), f"{fn.__name__} is not callable"

    def test_dispatcher_recognises_browser_tools(self):
        """BROWSER_* tool names must be in the dispatcher's known-tool set."""
        from harness.tools.dispatcher import _KNOWN_TOOLS
        expected = {
            "BROWSER_NAVIGATE", "BROWSER_SCREENSHOT", "BROWSER_CLICK",
            "BROWSER_FILL", "BROWSER_EVAL", "BROWSER_GET_TEXT",
        }
        missing = expected - set(_KNOWN_TOOLS)
        assert not missing, f"Missing from dispatcher: {missing}"

    def test_playwright_installed(self):
        """
        Playwright Python package must be installed.
        If missing, agents that call BROWSER_* will fail at runtime.
        """
        try:
            import playwright  # noqa: F401
        except ImportError:
            pytest.fail(
                "playwright package is not installed.\n"
                "Run: pip install playwright && playwright install chromium"
            )

    def test_browser_navigate_real(self, tmp_path):
        """
        Smoke test: actually open a local file:// URL with Playwright.
        Creates a minimal HTML file and navigates to it.
        """
        pytest.importorskip("playwright")

        html = tmp_path / "test.html"
        html.write_text("<html><body><h1>Harness E2E Test</h1></body></html>")
        url = html.as_uri()

        from harness.tools.browser import browser_navigate, browser_get_text, _session

        # Reset any existing session
        _session["run_id"] = None
        _session["playwright"] = None
        _session["browser"] = None
        _session["page"] = None

        run_id = "test-playwright-smoke"

        try:
            nav_result = browser_navigate(
                {"tool": "BROWSER_NAVIGATE", "url": url},
                run_id=run_id,
            )
            assert nav_result.get("status") == "ok", \
                f"Expected status 'ok', got: {nav_result}"
            assert nav_result.get("http_status") == 200, \
                f"Expected http_status 200, got: {nav_result}"

            text_result = browser_get_text(
                {"tool": "BROWSER_GET_TEXT", "selector": "h1"},
                run_id=run_id,
            )
            assert "Harness E2E Test" in text_result.get("text", ""), \
                f"Expected page content in text, got: {text_result}"
        finally:
            # Clean up playwright session
            try:
                if _session.get("browser"):
                    _session["browser"].close()
                if _session.get("playwright"):
                    _session["playwright"].stop()
            except Exception:
                pass
            _session["run_id"] = None
            _session["playwright"] = None
            _session["browser"] = None
            _session["page"] = None


# ---------------------------------------------------------------------------
# Test 4 — Accumulated issues flow
# ---------------------------------------------------------------------------

class TestAccumulatedIssues:
    """
    Issues found by one agent must appear in the task injected into the next agent.
    """

    def test_issues_propagate_to_next_agent(self):
        state = _state("qa_engineer")

        received_tasks: list[str] = []

        def fake_run_agent(agent_name, task, *a, **kw):
            received_tasks.append((agent_name, task))
            if agent_name == "qa_engineer":
                blk = _block("qa_engineer", "SUCCESS",
                              issues=["null pointer in auth.ts:42", "missing rate limit"])
                return blk, ""
            return _block(agent_name, "SUCCESS"), ""

        with patch("harness.pipeline.run_agent", side_effect=fake_run_agent), \
             patch("harness.pipeline._save"):
            _execute(state, PROJECT_ROOT, _noop_hooks(), converse=False)

        # code_reviewer should have received the issues
        cr_calls = [(a, t) for a, t in received_tasks if a == "code_reviewer"]
        assert cr_calls, "code_reviewer was never called"
        _, cr_task = cr_calls[0]
        assert "null pointer in auth.ts:42" in cr_task, \
            "qa_engineer issue not injected into code_reviewer task"
        assert "missing rate limit" in cr_task, \
            "qa_engineer issue not injected into code_reviewer task"
        assert "## Issues Found By Previous Agents" in cr_task
