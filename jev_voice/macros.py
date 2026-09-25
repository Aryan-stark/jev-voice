"""Task macros: one command, several verified steps.

This is where multi-step autonomy lives. Jev's job stays the thing it is good at --
choosing *which* macro you meant, from a closed set of names. The steps themselves are
ordinary code that checks its own work, so a macro is predictable and needs no planner.

Macros load from (first that exists):
    $JEV_MACROS
    ~/.config/jev-voice/macros.json
    <repo>/macros.json
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import actions, config, tools

MACRO_PATHS = [
    Path(os.environ["JEV_MACROS"]) if os.environ.get("JEV_MACROS") else None,
    Path.home() / ".config" / "jev-voice" / "macros.json",
    config.ROOT / "macros.json",
]

STEP_TIMEOUT = float(os.environ.get("MACRO_STEP_TIMEOUT", "6"))


def _path() -> Path | None:
    return next((p for p in MACRO_PATHS if p and p.exists()), None)


def load() -> dict[str, dict[str, Any]]:
    """{name: {description, steps}}. Never raises: a broken file must not stop voice control."""
    p = _path()
    if not p:
        return {}
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ! macros: {p} is unreadable ({e})")
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, body in (raw or {}).items():
        if isinstance(body, dict) and isinstance(body.get("steps"), list):
            out[str(name)] = {"description": str(body.get("description") or name),
                              "steps": body["steps"]}
    return out


def criteria() -> dict[str, str]:
    """Shaped for a Choice question: the macro names Jev can pick from."""
    return {name: body["description"] for name, body in load().items()}


# ---------------------------------------------------------------- execution

def _step(s: dict[str, Any]) -> tuple[bool, str]:
    """Run one step through the tool registry, which owns every capability."""
    kind = str(s.get("do") or "")
    args = {k: v for k, v in s.items() if k not in ("do", "settle")}
    t = tools.get(kind)
    if t is None:
        return False, f"unknown step {kind!r}"
    # Steps may carry a per-step timeout; only pass it to tools that accept one.
    if "timeout" in args and "timeout" not in t.run.__code__.co_varnames:
        args.pop("timeout")
    return tools.run(kind, args)


def run(name: str, dry: bool = False) -> tuple[bool, str]:
    """Run a macro's steps in order, stopping at the first step that cannot be verified.
    Returns (ok, spoken summary)."""
    macro = load().get(name)
    if not macro:
        return False, f"I don't have a {name} routine."
    steps = macro["steps"]
    if dry:
        return True, f"[dry] {name}: " + ", ".join(str(s.get('do')) for s in steps)
    done = 0
    for i, s in enumerate(steps, 1):
        try:
            ok, what = _step(s)
        except Exception as e:  # noqa: BLE001
            ok, what = False, f"{s.get('do')}: {e}"
        if not ok:
            return False, f"{name} stopped at step {i} of {len(steps)} ({what})."
        done += 1
        if s.get("do") not in ("wait", "wait_for_app"):
            time.sleep(float(s.get("settle", 0.25)))
    return True, f"{name.replace('_', ' ').capitalize()} done ({done} steps)."
