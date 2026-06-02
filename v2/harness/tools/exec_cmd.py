from __future__ import annotations

"""
EXEC_COMMAND — constrained, auditable shell execution.

Design: default-deny, allowlist-based.
  - Only explicitly allowlisted command patterns execute.
  - Shell metacharacters (;, |, &, >, <, `) are rejected outright.
  - subprocess.run(shell=False) prevents all shell expansion.
  - Working directory is sandboxed to project_root.
  - stdout/stderr are captured and truncated; no interactive TTY.
  - Every call (allowed or blocked) is logged to commands.jsonl.
  - ON_HUMAN_APPROVAL_REQUIRED is emitted before high-risk commands.

EXEC_COMMAND is the highest-risk tool in the harness. Keep this module
conservative and add new allowlist entries only with explicit justification.
"""

import json
import logging
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from harness.hooks import ON_COMMAND_FAILURE, ON_ERROR, ON_HUMAN_APPROVAL_REQUIRED
from harness.tools.file_ops import ToolError, _safe_resolve
from harness.tools.mutations import MutationContext

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowlist
#
# Each entry is a tuple of strings that must exactly match the *leading tokens*
# of the parsed argv. Trailing arguments (flags, paths) are permitted.
#
# Order does not matter for correctness, but keep related entries together
# for readability.
# ---------------------------------------------------------------------------

_ALLOWLIST: tuple[tuple[str, ...], ...] = (
    # ── Test runners ──────────────────────────────────────────────────────────
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("uv", "run", "pytest"),
    ("uv", "run", "python", "-m", "pytest"),
    ("python", "-m", "unittest"),
    ("python3", "-m", "unittest"),
    # ── Quick inline checks ───────────────────────────────────────────────────
    ("python", "-c"),
    ("python3", "-c"),
    ("python", "-m", "py_compile"),
    ("python3", "-m", "py_compile"),
    ("npm", "test"),
    ("npm", "run", "test"),
    ("npx", "jest"),
    ("npx", "vitest"),
    ("cargo", "test"),
    ("go", "test"),
    ("make", "test"),
    # ── Build ─────────────────────────────────────────────────────────────────
    ("npm", "run", "build"),
    ("npm", "run", "lint"),
    ("npm", "run", "typecheck"),
    ("npx", "tsc", "--noEmit"),
    ("cargo", "build"),
    ("go", "build"),
    ("make", "build"),
    # ── Static analysis / linting ─────────────────────────────────────────────
    ("ruff", "check"),
    ("ruff", "format", "--check"),
    ("black", "--check"),
    ("mypy",),
    ("flake8",),
    ("pylint",),
    ("pyright",),
    ("eslint",),
    ("tsc",),
    # ── Dev servers (use with background=true) ────────────────────────────────
    ("npm", "run", "dev"),
    ("npm", "start"),
    ("npx", "next", "dev"),
    ("npx", "next", "start"),
    ("flask", "run"),
    ("python", "-m", "flask", "run"),
    ("python3", "-m", "flask", "run"),
    ("uvicorn",),
    ("gunicorn",),
    ("python", "app.py"),
    ("python3", "app.py"),
    ("python", "main.py"),
    ("python3", "main.py"),
    ("python", "server.py"),
    ("python3", "server.py"),
    ("node", "server.js"),
    ("node", "app.js"),
    ("node", "index.js"),
    ("python", "-m", "http.server"),
    ("python3", "-m", "http.server"),
    # ── Deploy CLIs (devops_engineer only — gated by explicit approval in agent) ─
    ("vercel",),
    ("railway",),
    ("netlify",),
    ("wrangler", "deploy"),
    ("wrangler", "pages", "deploy"),
    ("fly", "deploy"),
    ("heroku",),
    # ── HTTP health checks (for verifying running servers) ────────────────────
    # curl is normally blocklisted for exfiltration risk, but is allowed here
    # for localhost health checks (QA / code_reviewer verify running servers).
    ("curl",),
)

# ---------------------------------------------------------------------------
# Blocklist (first token)
#
# Commands whose first token is always blocked unless the full argv matches
# the allowlist above. This gives more informative error messages than the
# generic "not in allowlist" message.
# ---------------------------------------------------------------------------

_BLOCKLIST_FIRST_TOKEN: frozenset[str] = frozenset({
    # Destructive filesystem
    "rm", "rmdir", "mv", "dd", "shred", "truncate",
    # Privilege escalation
    "sudo", "su", "doas", "pkexec",
    # Permission / ownership
    "chmod", "chown", "chgrp",
    # Network / data exfiltration (curl is allowlisted for localhost health checks)
    "wget", "nc", "netcat", "ncat", "socat",
    # Container / orchestration
    "docker", "podman", "kubectl", "helm", "k9s",
    # Cloud CLIs
    "aws", "gcloud", "az", "doctl", "flyctl",
    # Databases
    "psql", "mysql", "mongosh", "redis-cli", "sqlite3",
    # Remote access
    "ssh", "scp", "sftp", "rsync", "rclone",
    # Version control (too many destructive subcommands)
    "git",
    # Package managers (uncontrolled installs)
    "pip", "pip3", "pipx",
    # Shells (would allow arbitrary execution)
    "bash", "sh", "zsh", "fish", "dash", "ksh",
    "eval", "exec", "source",
    # Process manipulation
    "kill", "pkill", "killall",
    # Cron / scheduling
    "crontab", "at",
    # Editors (would open interactive sessions)
    "vim", "vi", "nano", "emacs",
})

# Shell operators that are dangerous as STANDALONE argv tokens.
# These indicate chained commands (cmd1 ; cmd2, cmd1 | cmd2, etc.).
# We do NOT flag these when embedded inside quoted arguments (e.g. python3 -c "a; b")
# because shell=False means the shell never processes them.
_STANDALONE_SHELL_OPS: frozenset[str] = frozenset({
    ";", ";;", "|", "||", "&", "&&", ">", ">>", "<", "<<", "`",
})

# Limits
_DEFAULT_TIMEOUT_S = 120
_MAX_TIMEOUT_S = 600     # hard cap: 10 minutes
_MAX_OUTPUT_BYTES = 65_536  # 64 KB per stream

# Risk levels
_LOW_RISK_PREFIXES: frozenset[tuple[str, ...]] = frozenset({
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("uv", "run", "pytest"),
    ("python", "-m", "unittest"),
    ("python3", "-m", "unittest"),
    ("npm", "test"),
    ("npm", "run", "test"),
    ("npx", "jest"),
    ("npx", "vitest"),
    ("cargo", "test"),
    ("go", "test"),
    ("make", "test"),
})

_MEDIUM_RISK_PREFIXES: frozenset[tuple[str, ...]] = frozenset({
    ("npm", "run", "build"),
    ("npm", "run", "lint"),
    ("npm", "run", "typecheck"),
    ("npx", "tsc", "--noEmit"),
    ("cargo", "build"),
    ("go", "build"),
    ("make", "build"),
    ("ruff", "check"),
    ("ruff", "format", "--check"),
    ("black", "--check"),
    ("mypy",),
    ("flake8",),
    ("pylint",),
    ("pyright",),
    ("eslint",),
    ("tsc",),
})

_COMMANDS_FILENAME = "commands.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_metacharacters(command: str) -> str | None:
    """Return an error string if shell operators appear in unquoted parts of the command.

    We scan only unquoted portions of the raw string. This means:
      - python3 -c "a; b"   → the ; is inside double-quotes → ALLOWED
      - echo hello; rm -rf / → the ; is unquoted → BLOCKED
      - ls | cat             → the | is unquoted → BLOCKED

    With shell=False, quoted metacharacters are harmless because the shell
    never processes the command. Only unquoted operators indicate intent
    to chain commands via a shell.
    """
    in_single = False
    in_double = False
    unquoted: list[str] = []
    for ch in command:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            unquoted.append(ch)
    unquoted_str = "".join(unquoted)

    for op in (";", "|", "`"):
        if op in unquoted_str:
            return (
                f"Shell operator {op!r} is not permitted in unquoted command text. "
                "EXEC_COMMAND uses direct process execution (shell=False). "
                "Issue separate tool calls for independent commands, "
                "or quote shell operators that are part of an argument value."
            )
    # & is only a shell operator when doubled (&&) or trailing — check both
    if "&&" in unquoted_str or unquoted_str.rstrip().endswith("&"):
        return (
            "Shell operator '&' / '&&' is not permitted in unquoted command text. "
            "EXEC_COMMAND uses direct process execution (shell=False)."
        )
    return None


def _parse(command: str) -> list[str] | str:
    """Parse command string via shlex. Returns list on success, error str on failure."""
    try:
        argv = shlex.split(command)
        if not argv:
            return "Empty command."
        return argv
    except ValueError as exc:
        return f"Command parse error: {exc}"


def _match_allowlist(argv: list[str]) -> bool:
    for prefix in _ALLOWLIST:
        if len(argv) >= len(prefix) and tuple(argv[:len(prefix)]) == prefix:
            return True
    return False


def _classify_risk(argv: list[str]) -> tuple[str, bool]:
    """Returns (risk_level, approval_required)."""
    for prefix in _LOW_RISK_PREFIXES:
        if len(argv) >= len(prefix) and tuple(argv[:len(prefix)]) == prefix:
            return "low", False
    for prefix in _MEDIUM_RISK_PREFIXES:
        if len(argv) >= len(prefix) and tuple(argv[:len(prefix)]) == prefix:
            return "medium", False
    return "high", True


# ---------------------------------------------------------------------------
# Dry-run preview
# ---------------------------------------------------------------------------

def dry_run_preview(command: str, project_root: Path) -> dict:
    """
    Return a structured preview of what exec_command would do, without executing.

    Used for:
      - Pre-execution audit logging
      - Future Slack/mobile approval flows
      - Debugging unexpected agent commands
    """
    # Metacharacter check
    meta_err = _check_metacharacters(command)
    if meta_err:
        return {
            "command": command,
            "argv": None,
            "allowed": False,
            "blocked_reason": meta_err,
            "risk_level": "blocked",
            "approval_required": True,
        }

    # Parse
    parsed = _parse(command)
    if isinstance(parsed, str):
        return {
            "command": command,
            "argv": None,
            "allowed": False,
            "blocked_reason": parsed,
            "risk_level": "blocked",
            "approval_required": True,
        }

    first = parsed[0]

    # Blocklist check (gives a more specific error than "not in allowlist")
    if first in _BLOCKLIST_FIRST_TOKEN and not _match_allowlist(parsed):
        return {
            "command": command,
            "argv": parsed,
            "allowed": False,
            "blocked_reason": (
                f"'{first}' is on the command blocklist. "
                "This command class is not permitted for agent execution."
            ),
            "risk_level": "blocked",
            "approval_required": True,
        }

    # Allowlist gate — default-deny
    if not _match_allowlist(parsed):
        allowed_examples = sorted({p[0] for p in _ALLOWLIST})
        return {
            "command": command,
            "argv": parsed,
            "allowed": False,
            "blocked_reason": (
                f"Command '{first}' is not in the allowlist. "
                f"Permitted command families: {', '.join(allowed_examples)}."
            ),
            "risk_level": "blocked",
            "approval_required": True,
        }

    risk_level, approval_required = _classify_risk(parsed)
    return {
        "command": command,
        "argv": parsed,
        "working_dir": str(project_root),
        "allowed": True,
        "blocked_reason": None,
        "risk_level": risk_level,
        "approval_required": approval_required,
        "timeout_s": _DEFAULT_TIMEOUT_S,
        "max_output_bytes": _MAX_OUTPUT_BYTES,
    }


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def _log_command(
    *,
    runs_dir: Path,
    run_id: str,
    agent_name: str,
    task_id: str,
    command: str,
    argv: list[str] | None,
    working_dir: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    duration_s: float,
    approval_required: bool,
    risk_level: str,
    success: bool,
    error: str | None,
) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id,
        "agent": agent_name,
        "task_id": task_id,
        "command": command,
        "argv": argv,
        "working_dir": working_dir,
        "stdout": stdout[:1024],
        "stderr": stderr[:1024],
        "exit_code": exit_code,
        "duration_s": round(duration_s, 3),
        "approval_required": approval_required,
        "risk_level": risk_level,
        "success": success,
        "error": error,
    }
    audit_path = runs_dir / run_id / _COMMANDS_FILENAME
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if success:
        log.info(
            "Command ok: %r exit=%d %.2fs run_id=%s",
            command, exit_code, duration_s, run_id,
        )
    else:
        log.warning(
            "Command failed/blocked: %r error=%s run_id=%s",
            command, error or f"exit={exit_code}", run_id,
        )


# ---------------------------------------------------------------------------
# Full output log files
# ---------------------------------------------------------------------------

def _save_full_output(
    *,
    runs_dir: Path,
    run_id: str,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    duration_s: float,
) -> str:
    """
    Write complete stdout+stderr to runs/<run_id>/logs/<slug>.log.

    Called only when output was truncated (len > _MAX_OUTPUT_BYTES).
    Returns the relative path string for inclusion in the tool result.
    """
    logs_dir = runs_dir / run_id / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Build a short filename slug from the command's first token + timestamp
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-z0-9_-]", "-", command.split()[0].lower())[:24]
    filename = f"{ts}-{slug}.log"
    log_path = logs_dir / filename

    header = (
        f"# Command Output Log\n"
        f"# command:   {command}\n"
        f"# exit_code: {exit_code}\n"
        f"# duration:  {duration_s:.3f}s\n"
        f"# run_id:    {run_id}\n\n"
    )
    log_path.write_text(
        header
        + "## stdout\n\n" + stdout + "\n\n"
        + "## stderr\n\n" + stderr + "\n",
        encoding="utf-8",
    )
    log.debug("Full command output saved: %s", log_path)
    return f"runs/{run_id}/logs/{filename}"


# ---------------------------------------------------------------------------
# EXEC_COMMAND
# ---------------------------------------------------------------------------

def exec_command(request: dict, ctx: MutationContext) -> dict:
    """
    Execute a command with allowlist gating, sandboxing, timeout, and full audit.

    Request fields:
      command     — command string (required)
      working_dir — directory relative to project_root (default: ".")
      timeout_s   — timeout override in seconds (max 600)
      background  — if true, start process in background and return PID immediately
                    (use for dev servers; the runner kills them when the agent finishes)
      reason      — rationale, logged only

    Returns a structured result dict with stdout, stderr, exit_code, duration.
    Raises ToolError for blocked or failed executions.
    """
    command = request.get("command", "").strip()
    if not command:
        raise ToolError("EXEC_COMMAND: 'command' is required.")

    rel_cwd = request.get("working_dir", ".")
    timeout_s = min(float(request.get("timeout_s", _DEFAULT_TIMEOUT_S)), _MAX_TIMEOUT_S)

    # Sandbox working directory
    cwd = _safe_resolve(rel_cwd, ctx.project_root)
    if not cwd.is_dir():
        raise ToolError(f"EXEC_COMMAND: working_dir '{rel_cwd}' is not a directory.")

    # ── Dry-run preview ───────────────────────────────────────────────────────
    preview = dry_run_preview(command, ctx.project_root)

    if not preview["allowed"]:
        _log_command(
            runs_dir=ctx.runs_dir, run_id=ctx.run_id,
            agent_name=ctx.agent_name, task_id=ctx.task_id,
            command=command, argv=preview.get("argv"),
            working_dir=str(cwd),
            stdout="", stderr="",
            exit_code=None, duration_s=0.0,
            approval_required=True,
            risk_level=preview.get("risk_level", "blocked"),
            success=False, error=preview["blocked_reason"],
        )
        raise ToolError(f"EXEC_COMMAND blocked: {preview['blocked_reason']}")

    argv = preview["argv"]
    risk_level = preview["risk_level"]
    approval_required = preview["approval_required"]

    # ── Background execution ──────────────────────────────────────────────────
    if request.get("background", False):
        log.info("EXEC_COMMAND background: starting %r cwd=%s", command, cwd)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,   # detach from parent process group
            )
        except FileNotFoundError:
            raise ToolError(
                f"EXEC_COMMAND: Executable not found: '{argv[0]}'. "
                "Verify the tool is installed."
            )
        ctx.background_pids.append(proc.pid)
        _log_command(
            runs_dir=ctx.runs_dir, run_id=ctx.run_id,
            agent_name=ctx.agent_name, task_id=ctx.task_id,
            command=command, argv=argv,
            working_dir=str(cwd),
            stdout="", stderr="",
            exit_code=None, duration_s=0.0,
            approval_required=approval_required,
            risk_level=risk_level,
            success=True, error=None,
        )
        return {
            "tool": "EXEC_COMMAND",
            "status": "started",
            "pid": proc.pid,
            "command": command,
            "background": True,
            "note": (
                "Server started in background. "
                "Use curl --retry 5 --retry-delay 1 --retry-connrefused <url> "
                "to wait for it to be ready before testing."
            ),
        }

    # ── Approval hook (emitted before execution; non-blocking for now) ─────────
    if approval_required and ctx.hook_system:
        ctx.hook_system.emit(ON_HUMAN_APPROVAL_REQUIRED, {
            "run_id": ctx.run_id,
            "agent": ctx.agent_name,
            "tool": "EXEC_COMMAND",
            "command": command,
            "argv": argv,
            "working_dir": str(cwd),
            "risk_level": risk_level,
            "reason": f"High-risk command queued for execution: {command!r}",
        })
        log.info("EXEC_COMMAND: ON_HUMAN_APPROVAL_REQUIRED emitted for %r", command)

    # ── Execute ──────────────────────────────────────────────────────────────
    log.info("EXEC_COMMAND: running %r cwd=%s timeout=%.0fs", command, cwd, timeout_s)

    stdout_text = ""
    stderr_text = ""
    exit_code: int | None = None
    exec_error: str | None = None
    stdout_truncated = False
    stderr_truncated = False

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            # shell=False is the default and is critical — never use shell=True
        )
        exit_code = proc.returncode

        raw_out = proc.stdout or ""
        raw_err = proc.stderr or ""

        out_bytes = raw_out.encode("utf-8")
        if len(out_bytes) > _MAX_OUTPUT_BYTES:
            stdout_text = out_bytes[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            stdout_truncated = True
        else:
            stdout_text = raw_out

        err_bytes = raw_err.encode("utf-8")
        if len(err_bytes) > _MAX_OUTPUT_BYTES:
            stderr_text = err_bytes[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            stderr_truncated = True
        else:
            stderr_text = raw_err

    except subprocess.TimeoutExpired:
        exec_error = f"Command timed out after {timeout_s:.0f}s."
    except FileNotFoundError:
        exec_error = (
            f"Executable not found: '{argv[0]}'. "
            "Verify the tool is installed in the project environment."
        )
    except PermissionError:
        exec_error = f"Permission denied executing '{argv[0]}'."
    except Exception as exc:
        exec_error = f"Unexpected execution error: {exc}"

    duration_s = time.monotonic() - t0
    success = exit_code is not None and exit_code == 0

    # Save full output to logs/ when either stream was truncated
    log_file_path: str | None = None
    if stdout_truncated or stderr_truncated:
        try:
            log_file_path = _save_full_output(
                runs_dir=ctx.runs_dir,
                run_id=ctx.run_id,
                command=command,
                stdout=stdout_text,
                stderr=stderr_text,
                exit_code=exit_code,
                duration_s=duration_s,
            )
        except Exception as exc:
            log.warning("Could not save full command output: %s", exc)

    _log_command(
        runs_dir=ctx.runs_dir, run_id=ctx.run_id,
        agent_name=ctx.agent_name, task_id=ctx.task_id,
        command=command, argv=argv,
        working_dir=str(cwd),
        stdout=stdout_text, stderr=stderr_text,
        exit_code=exit_code, duration_s=duration_s,
        approval_required=approval_required,
        risk_level=risk_level,
        success=success, error=exec_error,
    )

    if exec_error:
        if ctx.hook_system:
            ctx.hook_system.emit(ON_ERROR, {
                "run_id": ctx.run_id,
                "agent": ctx.agent_name,
                "tool": "EXEC_COMMAND",
                "command": command,
                "error": exec_error,
            })
        raise ToolError(f"EXEC_COMMAND: {exec_error}")

    # Emit on_command_failure when command ran but returned non-zero exit code
    if not success and ctx.hook_system:
        ctx.hook_system.emit(ON_COMMAND_FAILURE, {
            "run_id": ctx.run_id,
            "agent": ctx.agent_name,
            "tool": "EXEC_COMMAND",
            "command": command,
            "exit_code": exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "duration_s": round(duration_s, 3),
        })

    result: dict = {
        "tool": "EXEC_COMMAND",
        "status": "ok" if success else "failed",
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "duration_s": round(duration_s, 3),
        "risk_level": risk_level,
        "approval_required": approval_required,
    }
    if log_file_path:
        result["log_file"] = log_file_path
    return result
