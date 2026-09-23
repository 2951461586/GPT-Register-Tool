"""Machine-readable page truth to accompany every debug screenshot.

Why this exists
---------------
The debug screenshots under ``runtime/.../debug/`` are the first thing an
operator looks at when a flow stalls, and they are read by eye.  That makes them
(a) slow to compare between runs, (b) impossible to grep in an audit, and (c)
misleading when the image is not what the DOM actually says -- which is not
hypothetical: a compositing-stale frame, a full-page capture taken mid-navigation,
or an overlay that has already been dismissed all produce a picture that
contradicts the live page.

So every screenshot now gets a ``<name>.json`` sidecar holding the facts that were
true at capture time: URL, title, readyState, which dialogs were visible, and
which element had focus.  The PNG stays the human artifact; the JSON is the one
tooling and `git diff` can use.

Ordering is deliberate: the truth is collected **before** the screenshot is
written, and the sidecar records that.  If the page navigates in between, the
PNG is newer than the JSON -- and the sidecar says so rather than leaving the
reader to guess which one to believe.

Nothing here may raise.  It runs on failure paths, where an exception from the
diagnostic would replace the real error with a useless one.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: Injected into the page.  Kept as one expression so it works with Playwright's
#: ``evaluate`` (which wraps a string expression in a function body) and with any
#: other driver that accepts a snippet.
_TRUTH_SCRIPT = """(() => {
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], dialog[open]'))
    .filter(visible)
    .slice(0, 5)
    .map((el) => (el.getAttribute('aria-label') || el.id || el.tagName.toLowerCase()).slice(0, 80));
  const active = document.activeElement;
  return {
    url: location.href,
    title: document.title,
    readyState: document.readyState,
    visibleDialogs: dialogs,
    focusedElement: active ? (active.id || active.name || active.tagName.toLowerCase()).slice(0, 80) : null,
    formFieldCount: document.querySelectorAll('input, select, textarea').length,
  };
})()"""


def collect_page_truth(page: Any) -> dict[str, Any]:
    """Return what the DOM says right now, or ``{}`` when it cannot be read.

    Never raises: this is called on failure paths, and a diagnostic that throws
    replaces the real error with its own.
    """
    if page is None:
        return {}
    try:
        raw = page.evaluate(_TRUTH_SCRIPT)
    except Exception as exc:  # noqa: BLE001 - any driver failure is "no truth"
        _LOGGER.debug("could not read page truth: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def write_page_truth(page: Any, directory: Any, name: str) -> Path | None:
    """Write ``<name>.json`` next to the screenshot.  Returns the path, or None.

    The timestamp and the explicit ``capturedBeforeScreenshot`` marker are part
    of the record, not decoration: they are what lets a reader tell a stale
    sidecar from a stale PNG.
    """
    truth = collect_page_truth(page)
    if not truth:
        return None
    payload = {
        "name": str(name or ""),
        "capturedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capturedBeforeScreenshot": True,
        **truth,
    }
    try:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{name}.json"
        # ``newline="\n"`` on purpose: the default text mode rewrites "\n" as
        # "\r\n" on Windows, which makes the sidecar differ from run to run and
        # defeats the "diff two runs" reason this file exists.
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return path
    except OSError as exc:
        _LOGGER.debug("could not write page truth for %s: %s", name, exc)
        return None
