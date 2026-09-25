"""The capability registry: one definition per thing Jev can do.

Before this existed the executable surface was declared three times -- once as Jev's
action vocabulary, once as the planner's tool schemas, once as the macro runner's step
kinds -- and the names had already drifted (`open_website` / `open_url`, `take_note` /
`note`). Every new capability made that worse.

A Tool is declared once here and every consumer derives from it:

    planner.py   reads `json_schema()` to build its tool list
    macros.py    dispatches a step through `run()`
    main.py      reads `risk` / `reversible` to decide how careful to be

Registry names match the step kinds already used in macros.json, so existing routines
keep working unchanged.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable

from . import actions


class Risk(IntEnum):
    """How much damage a wrong call does. Drives the execution policy in main.py."""
    NONE = 0        # opening an app, switching a tab -- undo is "do the opposite"
    LOW = 1         # a note, a search -- clutter at worst
    MEDIUM = 2      # anything other people see, or that changes system state
    HIGH = 3        # destroys data
    CRITICAL = 4    # money, or irreversible external effects


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    run: Callable[..., tuple[bool, str]]
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    risk: Risk = Risk.LOW
    reversible: bool = False
    # Steps that need keyboard focus cannot run concurrently: `open -a` fights over
    # frontmost and keystrokes land wherever focus happens to be. Parallel planning
    # (roadmap P6) must respect this.
    needs_focus: bool = False
    # Kept out of the planner's tool list, but still runnable as a macro step.
    internal: bool = False

    def json_schema(self) -> dict[str, Any]:
        """OpenAI/Ollama-style function schema."""
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": self.params,
                           "required": list(self.required)},
        }}


_REGISTRY: dict[str, Tool] = {}


def register(t: Tool) -> Tool:
    _REGISTRY[t.name] = t
    return t


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def all_tools(include_internal: bool = False) -> list[Tool]:
    return [t for t in _REGISTRY.values() if include_internal or not t.internal]


def run(name: str, args: dict[str, Any]) -> tuple[bool, str]:
    """Execute one tool by name. Unknown tools fail loudly rather than silently."""
    t = get(name)
    if t is None:
        return False, f"unknown tool {name!r}"
    try:
        return t.run(**args)
    except TypeError as e:            # wrong/missing arguments from a planner
        return False, f"{name}: bad arguments ({e})"
    except Exception as e:            # noqa: BLE001
        return False, f"{name}: {e}"


STR = {"type": "string"}
INT = {"type": "integer"}


# ---------------------------------------------------------------- apps

def _open_app(app: str, timeout: float = 6.0) -> tuple[bool, str]:
    return actions.open_app(app, verify=timeout), f"open {app}"


def _focus_app(app: str, timeout: float = 6.0) -> tuple[bool, str]:
    return actions.focus_app(app, timeout=timeout), f"focus {app}"


def _wait_for_app(app: str, timeout: float = 6.0) -> tuple[bool, str]:
    ok = actions.wait_until(
        lambda: app.lower() in (a.lower() for a in actions.running_apps()), timeout)
    return ok, f"wait for {app}"


register(Tool("open_app", "Open or switch to an installed Mac app", _open_app,
              {"app": STR}, ("app",), Risk.NONE, reversible=False, needs_focus=True))
register(Tool("focus_app", "Bring an app to the front and wait until it is frontmost",
              _focus_app, {"app": STR}, ("app",), Risk.NONE, needs_focus=True, internal=True))
register(Tool("wait_for_app", "Wait until an app is running", _wait_for_app,
              {"app": STR}, ("app",), Risk.NONE, internal=True))


# ---------------------------------------------------------------- web

def _open_url(url: str) -> tuple[bool, str]:
    actions.open_url(url)
    return True, f"open {url}"


def _new_tab(url: str) -> tuple[bool, str]:
    return actions.open_in_new_tab(url), f"tab {url}"


def _switch_tab(win: int | None = None, tab: int | None = None,
                title: str = "") -> tuple[bool, str]:
    if win is None or tab is None:            # planner supplies a title only
        hit = next((t for t in actions.browser_tabs()
                    if title and title.lower() in t[2].lower()), None)
        if hit is None:
            return False, f"no open tab matching {title!r}"
        win, tab, title = hit
    return actions.activate_tab(int(win), int(tab)), f"tab {str(title)[:30]}"


def _close_tab(win: int, tab: int, title: str = "") -> tuple[bool, str]:
    return actions.close_tab(int(win), int(tab)), f"closed {str(title)[:30]}"


def _web_search(query: str, engine: str = "google") -> tuple[bool, str]:
    actions.web_search(engine, query)
    return True, f"search {query[:30]}"


register(Tool("open_url", "Open a web page in the default browser", _open_url,
              {"url": STR}, ("url",), Risk.NONE, needs_focus=True))
register(Tool("new_tab", "Open a URL in a new browser tab", _new_tab,
              {"url": STR}, ("url",), Risk.NONE))
register(Tool("switch_tab", "Switch to an ALREADY-OPEN browser tab by its title",
              _switch_tab, {"title": STR}, ("title",), Risk.NONE, reversible=True,
              needs_focus=True))
register(Tool("close_tab", "Close an open browser tab", _close_tab,
              {"title": STR}, (), Risk.LOW, reversible=True, internal=True))
register(Tool("web_search", "Search the web", _web_search,
              {"query": STR, "engine": STR}, ("query",), Risk.LOW, needs_focus=True))


# ---------------------------------------------------------------- capture

def _note(text: str) -> tuple[bool, str]:
    actions.note_capture(text)
    return True, "note"


def _reminder(text: str) -> tuple[bool, str]:
    actions.reminder_capture(text)
    return True, "reminder"


register(Tool("note", "Create a note in the Notes app", _note,
              {"text": STR}, ("text",), Risk.LOW, reversible=False))
register(Tool("reminder", "Add a to-do item to Reminders", _reminder,
              {"text": STR}, ("text",), Risk.LOW))


# ---------------------------------------------------------------- input

def _type(text: str) -> tuple[bool, str]:
    actions.type_text(text)
    return True, "type"


def _shortcut(key: str, times: int = 1) -> tuple[bool, str]:
    if key not in actions.SHORTCUTS:
        return False, f"unknown shortcut {key!r}"
    actions.press(key, times=int(times))
    return True, f"press {key}"


def _scroll(direction: str = "down", amount: str = "page") -> tuple[bool, str]:
    actions.scroll(direction, amount)
    return True, f"scroll {direction}"


register(Tool("type", "Type text into whatever field currently has focus", _type,
              {"text": STR}, ("text",), Risk.MEDIUM, needs_focus=True))
register(Tool("shortcut", "Press a keyboard shortcut by name", _shortcut,
              {"key": STR}, ("key",), Risk.MEDIUM, needs_focus=True))
register(Tool("scroll", "Scroll the current window", _scroll,
              {"direction": STR, "amount": STR}, ("direction",), Risk.NONE,
              needs_focus=True))


# ---------------------------------------------------------------- system

def _volume(level: int) -> tuple[bool, str]:
    actions.set_volume(int(level))
    return True, f"volume {level}"


def _system(op: str) -> tuple[bool, str]:
    if op not in ("lock", "sleep_display", "show_desktop", "toggle_dark_mode", "empty_trash"):
        return False, f"unknown system op {op!r}"
    return True, actions.system(op) or f"system {op}"


def _screenshot() -> tuple[bool, str]:
    return True, f"screenshot {actions.screenshot().name}"


def _open_folder(folder: str) -> tuple[bool, str]:
    if folder not in actions.FOLDERS:
        return False, f"unknown folder {folder!r}"
    actions.open_folder(folder)
    return True, f"folder {folder}"


def _media(op: str) -> tuple[bool, str]:
    actions.media(op)
    return True, f"media {op}"


def _wait(seconds: float = 0.5) -> tuple[bool, str]:
    time.sleep(min(float(seconds), 10.0))
    return True, "wait"


register(Tool("volume", "Set system output volume, 0 to 100", _volume,
              {"level": INT}, ("level",), Risk.NONE, reversible=True))
# empty_trash destroys data, but `system` is one tool -- main.py gates on the op.
register(Tool("system", "A system action: lock, sleep_display, show_desktop, "
                        "toggle_dark_mode, empty_trash", _system,
              {"op": STR}, ("op",), Risk.MEDIUM))
register(Tool("screenshot", "Take a screenshot of the screen, saved to the Desktop",
              _screenshot, {}, (), Risk.NONE))
register(Tool("open_folder", "Open a folder in Finder", _open_folder,
              {"folder": STR}, ("folder",), Risk.NONE, needs_focus=True))
register(Tool("media", "Control playback: play_pause, next, previous", _media,
              {"op": STR}, ("op",), Risk.NONE))
register(Tool("wait", "Pause for a number of seconds", _wait,
              {"seconds": INT}, (), Risk.NONE, internal=True))


# ---------------------------------------------------------------- slack

def _slack(to: str, text: str, send: bool = True) -> tuple[bool, str]:
    ok, why = actions.slack_send(to, text, send=bool(send))
    return ok, f"slack {to}: {why}"


register(Tool("slack", "Send a Slack message to a person (@name) or channel (#name)",
              _slack, {"to": STR, "text": STR}, ("to", "text"),
              Risk.MEDIUM, reversible=False, needs_focus=True))


# Ops within the `system` tool that are more dangerous than the tool's own risk level.
OP_RISK: dict[tuple[str, str], Risk] = {
    ("system", "empty_trash"): Risk.HIGH,      # permanent: nothing can undo this
    ("system", "lock"): Risk.MEDIUM,
    ("system", "sleep_display"): Risk.MEDIUM,
    # Shortcuts that lose work or close things irreversibly.
    ("shortcut", "quit_app"): Risk.HIGH,
    ("shortcut", "close_tab_or_window"): Risk.MEDIUM,
    ("shortcut", "delete_line"): Risk.HIGH,
    ("shortcut", "delete_word"): Risk.MEDIUM,
}


def risk_of(name: str, args: dict[str, Any]) -> Risk:
    """Risk of a specific call, which can exceed the tool's baseline."""
    t = get(name)
    if t is None:
        return Risk.HIGH
    for key in ("op", "key", "folder"):
        if key in args:
            override = OP_RISK.get((name, str(args[key])))
            if override is not None:
                return max(t.risk, override)
    return t.risk
