"""Escalation tier: turn an arbitrary request into a plan of known steps.

Jev is a classifier over ~20 fixed actions. Asked for something outside that set it
picks the nearest neighbour and runs it -- which is how "mute slack until 3pm" ends up
locking the screen. This module is the fallback for exactly those cases.

A local model (Ollama, free and offline) is given the SAME primitives the macro runner
executes, and answers with tool calls. Two properties matter more than cleverness:

  1. It can emit `cannot_do`, so an impossible request is refused instead of approximated.
  2. Its output is plain steps, executed by the existing verified runner -- the model
     never touches the machine, so it cannot invent an action that isn't already code.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from . import actions, tools

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
# Must be a NON-thinking model. Qwen3's hybrid models reason in the visible content
# stream even with think=false, which costs 7-25s per plan; the -instruct-2507 line has
# no thinking mode at all and answers in ~1s with better accuracy.
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
PLANNER_TIMEOUT = float(os.environ.get("PLANNER_TIMEOUT", "25"))
PLANNER_ENABLED = os.environ.get("PLANNER", "1") not in ("0", "false", "no")
MAX_STEPS = 8
STR_SCHEMA = {"type": "string"}

SYSTEM = (
    "You turn a spoken request into a short sequence of tool calls that control a macOS "
    "machine. Call only the tools listed, in the order they should run. Keep it minimal: "
    "the fewest steps that accomplish the request.\n"
    "If the request cannot be accomplished with these tools, call cannot_do and say why "
    "in one short sentence. Never invent a tool. Never guess at a capability you do not "
    "have. Refusing is always better than doing something close but wrong.\n"
    "A request joined by 'and' or 'then' usually needs ONE CALL PER PART -- "
    "'open chrome and take a note saying hi' is two calls: open_app, then note. "
    "Do not drop a part because you already made one call."
)

# Each tool maps 1:1 onto a step kind that jev_voice.macros already knows how to run.
def _tools(apps: list[str], tabs: list[str]) -> list[dict[str, Any]]:
    """The planner's tool list is derived from the registry, so a capability added in
    tools.py is immediately plannable -- no second declaration to keep in sync."""
    app_hint = ", ".join(apps[:40])
    tab_hint = "; ".join(tabs[:15]) or "(no browser tabs open)"
    hints = {
        "open_app": f" Installed: {app_hint}",
        "switch_tab": f" Open tabs: {tab_hint}",
        "open_folder": " One of: " + ", ".join(actions.FOLDERS),
        "shortcut": " One of: " + ", ".join(list(actions.SHORTCUTS)[:18]),
        "web_search": " Engines: " + ", ".join(actions.SEARCH_ENGINES),
    }
    out: list[dict[str, Any]] = []
    for t in tools.all_tools():
        schema = t.json_schema()
        if t.name in hints:
            schema["function"]["description"] += hints[t.name]
        out.append(schema)
    out.append({"type": "function", "function": {
        "name": "cannot_do",
        "description": ("The request cannot be done with these tools. Always prefer this "
                        "over a close approximation."),
        "parameters": {"type": "object", "properties": {"reason": STR_SCHEMA},
                       "required": ["reason"]},
    }})
    return out


_FALLBACK_REASON = "that isn't something I can do"


def _reason_from_text(text: str) -> str:
    """Pull a human sentence out of a prose refusal, which may wrap or embed JSON.
    Small models often emit cannot_do as text rather than a tool call."""
    text = (text or "").strip()
    if not text:
        return _FALLBACK_REASON
    start = text.find("{")
    if start != -1:
        try:
            obj = json.loads(text[start:text.rindex("}") + 1])
            if isinstance(obj, dict) and obj.get("reason"):
                return str(obj["reason"]).strip()
        except (json.JSONDecodeError, ValueError):
            pass
        text = text[:start].strip()
    for line in text.splitlines():
        line = line.strip()
        if line and line != "cannot_do" and not line.startswith(("{", "}")):
            return line[:140]
    return _FALLBACK_REASON


def model_name() -> str:
    return PLANNER_MODEL


def available() -> bool:
    if not PLANNER_ENABLED:
        return False
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=1.5) as r:
            names = {m["name"] for m in json.loads(r.read()).get("models", [])}
        return PLANNER_MODEL in names or any(n.startswith(PLANNER_MODEL.split(":")[0]) for n in names)
    except Exception:
        return False


def plan(utterance: str) -> tuple[list[dict[str, Any]], str | None]:
    """Return (steps, refusal). Exactly one is meaningful: a refusal means do nothing."""
    apps = actions.running_apps() + actions.installed_apps()[:30]
    tabs = [t[2] for t in actions.browser_tabs()] if actions.current_browser() else []
    body = json.dumps({
        "model": PLANNER_MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": utterance}],
        "tools": _tools(sorted(set(apps)), tabs),
        "stream": False, "think": False,
        "options": {"temperature": 0},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=PLANNER_TIMEOUT) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return [], f"the planner is unavailable ({type(e).__name__})"

    msg = data.get("message") or {}
    calls = msg.get("tool_calls") or []
    if not calls:
        # Small models often emit the refusal as prose rather than a call, sometimes with
        # the tool name and raw JSON around it. Dig the sentence out of whatever we got.
        return [], _reason_from_text(msg.get("content") or "")

    steps: list[dict[str, Any]] = []
    for c in calls[:MAX_STEPS]:
        fn = c.get("function") or {}
        name = fn.get("name")
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if name == "cannot_do":
            return [], str(args.get("reason") or "that isn't something I can do")
        elif tools.get(name) is not None:
            steps.append({"do": name, **args})
        # anything else is a hallucinated tool: drop it rather than guess
    if not steps:
        return [], "that isn't something I can do"
    return steps, None
