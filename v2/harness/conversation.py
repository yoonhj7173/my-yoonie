from __future__ import annotations

"""
conversation.py — Interactive conversation loop for the harness.

When an agent returns NEEDS_USER_INPUT, the pipeline can call
enter_conversation_loop() instead of just pausing. This function:
  1. Streams the agent's current response to stdout
  2. Reads user input from stdin
  3. Injects the input as the next turn and re-runs the agent
  4. Repeats until the agent returns SUCCESS/FEATURE_COMPLETE/FAILED/BLOCKED
  5. Returns the final StatusBlock so the pipeline can continue

Both software_engineer (inline, same agent continues) and
debugger_engineer (standalone, called by code_reviewer/QA) use this.
"""

import logging
import sys
from pathlib import Path

from harness.hooks import HookSystem, hooks as default_hooks
from harness.runner import run_agent
from harness.status import StatusBlock, StatusCode

log = logging.getLogger(__name__)

_CONVERSATION_SENTINEL = "CONVERSATION_DONE"
_DONE_STATUSES = {StatusCode.SUCCESS, StatusCode.FEATURE_COMPLETE, StatusCode.FAILED, StatusCode.BLOCKED}


def _print_divider(char: str = "─", width: int = 60) -> None:
    print(char * width, flush=True)


def _stream_to_stdout(text: str) -> None:
    """Print agent response with a visual prefix."""
    print(f"\n\033[1;36m[harness]\033[0m {text}\n", flush=True)


def _read_user_input() -> str | None:
    """
    Read user input from stdin.
    Returns None if the user types 'exit', 'quit', or sends EOF.
    """
    _print_divider()
    print("\033[1;33m[you]\033[0m ", end="", flush=True)
    try:
        line = input().strip()
    except EOFError:
        return None
    if line.lower() in ("exit", "quit", "q"):
        return None
    return line


def enter_conversation_loop(
    agent_name: str,
    task: str,
    provider_name: str,
    project_root: Path,
    run_id: str,
    initial_summary: str = "",
    hook_system: HookSystem | None = None,
) -> StatusBlock:
    """
    Run an interactive multi-turn conversation with agent_name.

    The agent runs, streams its response, reads user input, injects it as
    a follow-up turn, and loops until the agent signals it is done
    (SUCCESS / FEATURE_COMPLETE / FAILED / BLOCKED).

    Returns the final StatusBlock so the caller (pipeline) can route accordingly.
    """
    hs = hook_system or default_hooks

    _print_divider("═")
    print(f"\033[1;35m[conversation mode]\033[0m  agent: {agent_name}")
    if initial_summary:
        print(f"\033[1;35m[reason]\033[0m  {initial_summary}")
    _print_divider("═")
    print("Type your response and press Enter. Type 'exit' to abort.\n", flush=True)

    # Conversation history — list of {role, content} dicts passed to the provider
    history: list[dict] = []

    current_task = task
    turn = 0
    final_block: StatusBlock | None = None

    while True:
        turn += 1
        log.info("Conversation turn %d  agent=%s", turn, agent_name)

        def _on_token(token: str) -> None:
            sys.stdout.write(token)
            sys.stdout.flush()

        print(f"\n\033[1;36m[{agent_name}]\033[0m thinking...\n", flush=True)

        status_block, response_text = run_agent(
            agent_name=agent_name,
            task=current_task,
            provider_name=provider_name,
            project_root=project_root,
            run_id=run_id,
            hook_system=hs,
            stream_callback=_on_token,
            history=history if history else None,
        )

        final_block = status_block

        # Accumulate history so the next turn has full context
        history.append({"role": "assistant", "content": response_text})

        # If the agent is done, exit the loop
        if status_block.status in _DONE_STATUSES:
            _print_divider("═")
            print(
                f"\033[1;35m[conversation done]\033[0m  "
                f"status={status_block.status.value}  summary={status_block.summary}",
                flush=True,
            )
            _print_divider("═")
            break

        # Agent signalled it needs user input — use Slack if registered, else terminal
        from harness.integrations import input_router  # noqa: PLC0415
        _input_fn = input_router.get()
        if _input_fn is not None:
            user_input = _input_fn(status_block.next_recommended_action or status_block.summary or "")
        else:
            user_input = _read_user_input()
        if user_input is None:
            log.info("User exited conversation — treating as BLOCKED")
            from harness.status import generate_run_id, generate_task_id  # noqa: PLC0415
            final_block = StatusBlock(
                run_id=run_id,
                task_id=status_block.task_id,
                agent=agent_name,
                status=StatusCode.BLOCKED,
                summary="User exited conversation loop.",
                next_recommended_action="Resume and provide user input.",
            )
            break

        # Inject user turn into history and update task for next agent call
        history.append({"role": "user", "content": user_input})

        # Also append to task so it's visible in prompt even if history is not supported
        current_task = (
            current_task
            + f"\n\n---\n\n## User Follow-up (turn {turn})\n\n{user_input}"
        )
        log.info("User input injected  turn=%d  length=%d", turn, len(user_input))

    return final_block
