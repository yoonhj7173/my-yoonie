from __future__ import annotations

"""
BROWSER_* tools — Playwright-based browser automation for QA and code review.

Tools provided:
  BROWSER_NAVIGATE   — navigate to a URL, return page title and status
  BROWSER_SCREENSHOT — take a screenshot, save to runs/<run_id>/screenshots/
  BROWSER_CLICK      — click an element by CSS selector
  BROWSER_FILL       — fill an input by CSS selector
  BROWSER_EVAL       — evaluate JavaScript and return the result
  BROWSER_GET_TEXT   — get text content of an element by CSS selector

Browser session is a module-level singleton scoped to the current run_id.
A new session is created when run_id changes or when the browser crashes.

Requires: pip install playwright && playwright install chromium
"""

import logging
from pathlib import Path
from typing import Any

from harness.tools.file_ops import ToolError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

_session: dict[str, Any] = {
    "run_id": None,
    "playwright": None,
    "browser": None,
    "page": None,
}


def _ensure_session(run_id: str) -> Any:
    """Return the Playwright page for run_id, creating a new session if needed."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        raise ToolError(
            "playwright is not installed. "
            "Run: pip install playwright && playwright install chromium"
        )

    if _session["run_id"] == run_id and _session["page"] is not None:
        # Quick liveness check
        try:
            _ = _session["page"].url
            return _session["page"]
        except Exception:
            _close_session()

    _close_session()

    log.info("Starting new Playwright browser session for run_id=%s", run_id)
    pw = sync_playwright().__enter__()
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()

    _session["run_id"] = run_id
    _session["playwright"] = pw
    _session["browser"] = browser
    _session["page"] = page

    return page


def _close_session() -> None:
    try:
        if _session["browser"] is not None:
            _session["browser"].close()
    except Exception:
        pass
    try:
        if _session["playwright"] is not None:
            _session["playwright"].stop()
    except Exception:
        pass
    _session.update(run_id=None, playwright=None, browser=None, page=None)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def browser_navigate(request: dict, run_id: str) -> dict:
    url = request.get("url", "")
    if not url:
        return {"tool": "BROWSER_NAVIGATE", "status": "error", "error": "No 'url' provided."}

    wait_until = request.get("wait_until", "load")  # load | networkidle | domcontentloaded
    timeout = int(request.get("timeout_ms", 15000))

    try:
        page = _ensure_session(run_id)
        response = page.goto(url, wait_until=wait_until, timeout=timeout)
        status = response.status if response else 0
        title = page.title()
        current_url = page.url
        return {
            "tool": "BROWSER_NAVIGATE",
            "status": "ok",
            "url": current_url,
            "http_status": status,
            "title": title,
        }
    except ToolError:
        raise
    except Exception as exc:
        return {"tool": "BROWSER_NAVIGATE", "status": "error", "error": str(exc)}


def browser_screenshot(request: dict, run_id: str, runs_dir: Path) -> dict:
    filename = request.get("filename", "screenshot.png")
    # Force png extension for consistency
    if not filename.endswith(".png"):
        filename = filename.rsplit(".", 1)[0] + ".png"

    save_dir = runs_dir / run_id / "screenshots"
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / filename

    full_page = bool(request.get("full_page", False))

    try:
        page = _ensure_session(run_id)
        page.screenshot(path=str(save_path), full_page=full_page)
        return {
            "tool": "BROWSER_SCREENSHOT",
            "status": "ok",
            "path": str(save_path.relative_to(runs_dir.parent)),
            "filename": filename,
        }
    except ToolError:
        raise
    except Exception as exc:
        return {"tool": "BROWSER_SCREENSHOT", "status": "error", "error": str(exc)}


def browser_click(request: dict, run_id: str) -> dict:
    selector = request.get("selector", "")
    if not selector:
        return {"tool": "BROWSER_CLICK", "status": "error", "error": "No 'selector' provided."}

    timeout = int(request.get("timeout_ms", 5000))

    try:
        page = _ensure_session(run_id)
        page.click(selector, timeout=timeout)
        return {
            "tool": "BROWSER_CLICK",
            "status": "ok",
            "selector": selector,
            "current_url": page.url,
        }
    except ToolError:
        raise
    except Exception as exc:
        return {"tool": "BROWSER_CLICK", "status": "error", "error": str(exc)}


def browser_fill(request: dict, run_id: str) -> dict:
    selector = request.get("selector", "")
    text = request.get("text", "")
    if not selector:
        return {"tool": "BROWSER_FILL", "status": "error", "error": "No 'selector' provided."}

    timeout = int(request.get("timeout_ms", 5000))

    try:
        page = _ensure_session(run_id)
        page.fill(selector, text, timeout=timeout)
        return {
            "tool": "BROWSER_FILL",
            "status": "ok",
            "selector": selector,
            "text_length": len(text),
        }
    except ToolError:
        raise
    except Exception as exc:
        return {"tool": "BROWSER_FILL", "status": "error", "error": str(exc)}


def browser_eval(request: dict, run_id: str) -> dict:
    code = request.get("code", "")
    if not code:
        return {"tool": "BROWSER_EVAL", "status": "error", "error": "No 'code' provided."}

    try:
        page = _ensure_session(run_id)
        result = page.evaluate(code)
        return {
            "tool": "BROWSER_EVAL",
            "status": "ok",
            "result": result,
        }
    except ToolError:
        raise
    except Exception as exc:
        return {"tool": "BROWSER_EVAL", "status": "error", "error": str(exc)}


def browser_get_text(request: dict, run_id: str) -> dict:
    selector = request.get("selector", "")
    if not selector:
        return {"tool": "BROWSER_GET_TEXT", "status": "error", "error": "No 'selector' provided."}

    timeout = int(request.get("timeout_ms", 5000))

    try:
        page = _ensure_session(run_id)
        element = page.wait_for_selector(selector, timeout=timeout)
        if element is None:
            return {
                "tool": "BROWSER_GET_TEXT",
                "status": "error",
                "error": f"No element found for selector: {selector!r}",
            }
        text = element.text_content() or ""
        return {
            "tool": "BROWSER_GET_TEXT",
            "status": "ok",
            "selector": selector,
            "text": text.strip(),
        }
    except ToolError:
        raise
    except Exception as exc:
        return {"tool": "BROWSER_GET_TEXT", "status": "error", "error": str(exc)}
