"""Thin Accessibility (AX) wrapper.

Slack has no AppleScript interface, so driving it means keystrokes -- and keystrokes
without verification are how you send a message to the wrong person. This module reads
the AX tree so each step can be confirmed before the next one runs.

Everything here needs Accessibility permission and returns None/False without it.
"""
from __future__ import annotations

import time
from typing import Any, Callable


def trusted() -> bool:
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore

        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def pid_of(app_name: str) -> int | None:
    try:
        from AppKit import NSWorkspace  # type: ignore

        for a in NSWorkspace.sharedWorkspace().runningApplications():
            if a.localizedName() and str(a.localizedName()).lower() == app_name.lower():
                return int(a.processIdentifier())
    except Exception:
        pass
    return None


def _attr(el: Any, name: str) -> Any:
    from ApplicationServices import AXUIElementCopyAttributeValue  # type: ignore

    try:
        err, val = AXUIElementCopyAttributeValue(el, name, None)
        return val if err == 0 else None
    except Exception:
        return None


def app_element(app_name: str) -> Any:
    from ApplicationServices import AXUIElementCreateApplication  # type: ignore

    pid = pid_of(app_name)
    if pid is None:
        return None
    try:
        return AXUIElementCreateApplication(pid)
    except Exception:
        return None


def focused(app_name: str) -> Any:
    el = app_element(app_name)
    return _attr(el, "AXFocusedUIElement") if el else None


def focused_info(app_name: str) -> dict[str, str]:
    """Role, value and placeholder of whatever has keyboard focus inside the app.
    The placeholder is the useful one: Slack's composer reads 'Message #general'."""
    el = focused(app_name)
    if el is None:
        return {}
    out = {}
    for key, attr in (("role", "AXRole"), ("value", "AXValue"),
                      ("placeholder", "AXPlaceholderValue"), ("desc", "AXDescription"),
                      ("title", "AXTitle")):
        v = _attr(el, attr)
        if v is not None:
            out[key] = str(v)
    return out


def window_title(app_name: str) -> str:
    """Title of the app's main window. For Slack this names the open conversation
    ('general (Channel) - Workspace - Slack'), which is the most reliable signal
    available for confirming the right conversation is open before typing."""
    el = app_element(app_name)
    if el is None:
        return ""
    win = _attr(el, "AXMainWindow") or _attr(el, "AXFocusedWindow")
    if win is None:
        kids = _attr(el, "AXChildren") or []
        win = next((k for k in kids if _attr(k, "AXRole") == "AXWindow"), None)
    return str(_attr(win, "AXTitle") or "") if win is not None else ""


def wait_until(pred: Callable[[], bool], timeout: float, interval: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def dump(app_name: str, depth: int = 3) -> list[str]:
    """Probe helper: shallow walk of the AX tree, for working out what is readable."""
    el = app_element(app_name)
    if el is None:
        return ["<app not running or AX unavailable>"]
    lines: list[str] = []

    def walk(node: Any, d: int, path: str) -> None:
        if d > depth or len(lines) > 200:
            return
        role = _attr(node, "AXRole")
        title = _attr(node, "AXTitle") or _attr(node, "AXDescription")
        ph = _attr(node, "AXPlaceholderValue")
        lines.append(f"{'  ' * d}{path} {role} title={title!r} placeholder={ph!r}")
        kids = _attr(node, "AXChildren") or []
        for i, k in enumerate(list(kids)[:12]):
            walk(k, d + 1, f"{path}.{i}")

    walk(el, 0, "root")
    return lines
