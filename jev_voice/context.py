"""What the computer looks like right now.

"Send this to him" only resolves if the machine's state is part of the question, not
just the sentence. This gathers that state.

Two rules shape the design:

1. **Gather lazily.** Every field is a cached property, so asking for the frontmost app
   never pays for an AppleScript round-trip to enumerate browser tabs. `for_prompt()`
   collects only the fields a given request could plausibly need.

2. **Prefer the cheapest source that works.** Measured on this machine: running apps and
   the frontmost app are free; window titles need Accessibility; browser tabs need
   per-app Automation; the system-wide AX focused element is simply unavailable, and
   per-app focused reads come back empty on Electron apps. So selected text falls back
   to a clipboard round-trip rather than pretending AX will answer.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from . import actions, ax

# A clipboard round-trip to read the selection presses Cmd+C, which is intrusive
# enough that it is opt-in per request rather than gathered by default.
SELECTION_VIA_CLIPBOARD = os.environ.get("SELECTION_VIA_CLIPBOARD", "1") not in ("0", "false", "no")
MAX_TABS_IN_PROMPT = 12
MAX_TEXT = 600


@dataclass
class Context:
    """A snapshot of the machine, gathered on demand.

    Construct one per utterance -- the cached properties make repeated access free,
    but a stale snapshot is worse than none.
    """
    rt: Any = None                       # Runtime, for recent turns
    _taken: float = field(default_factory=time.monotonic)

    # ---------------------------------------------------------- free

    @cached_property
    def frontmost_app(self) -> str:
        return actions.frontmost_app()

    @cached_property
    def running_apps(self) -> list[str]:
        return actions.running_apps()

    @cached_property
    def clipboard(self) -> str:
        return (actions.get_clipboard() or "")[:MAX_TEXT]

    @cached_property
    def recent_turns(self) -> list[dict[str, Any]]:
        return self.rt.recent() if self.rt is not None else []

    # ---------------------------------------------------------- needs Accessibility

    @cached_property
    def active_window(self) -> str:
        """Window title of the frontmost app. On Slack this names the open
        conversation, which is the only reliable way to know who "him" is."""
        app = self.frontmost_app
        return ax.window_title(app) if app else ""

    @cached_property
    def focused_element(self) -> dict[str, str]:
        """Often empty -- Electron apps expose little here. Callers must cope."""
        app = self.frontmost_app
        return ax.focused_info(app) if app else {}

    # ---------------------------------------------------------- needs Automation

    @cached_property
    def browser(self) -> str | None:
        return actions.current_browser()

    @cached_property
    def open_tabs(self) -> list[tuple[int, int, str]]:
        return actions.browser_tabs() if self.browser else []

    @cached_property
    def active_tab(self) -> str:
        for win, tab, title in self.open_tabs:
            if win == 1 and tab == 1:
                return title
        return self.open_tabs[0][2] if self.open_tabs else ""

    # ---------------------------------------------------------- intrusive

    @cached_property
    def selected_text(self) -> str:
        """The current selection.

        AX would be the clean route, but the system-wide focused element is
        unavailable and per-app reads return empty on the apps that matter, so this
        falls back to Cmd+C and reads the pasteboard back -- restoring it afterwards,
        and only treating the result as a selection if the clipboard actually changed.
        """
        info = self.focused_element
        if info.get("selected"):
            return str(info["selected"])[:MAX_TEXT]
        if not SELECTION_VIA_CLIPBOARD:
            return ""
        before_count, items = actions._pb_snapshot()
        before_text = actions.get_clipboard()
        try:
            actions.press("copy")
        except Exception:  # noqa: BLE001 - no Accessibility, or nothing focused
            return ""
        # Cmd+C on an empty selection is a no-op, so an unchanged pasteboard means
        # "nothing was selected" rather than "the selection equals the clipboard".
        changed = actions.wait_until(
            lambda: actions._pasteboard().changeCount() != before_count, 0.4, 0.02)
        text = actions.get_clipboard() or ""
        if not changed:
            return ""
        got = text[:MAX_TEXT]
        if items:
            actions._pb_restore(actions._pasteboard().changeCount(), items)
        elif before_text is None:
            pass
        return got

    # ---------------------------------------------------------- shaping

    def summary(self) -> str:
        """One line for logs and the overlay."""
        bits = [f"app={self.frontmost_app or '?'}"]
        if self.active_window:
            bits.append(f"window={self.active_window.split(' - ')[0][:40]!r}")
        if self.open_tabs:
            bits.append(f"tabs={len(self.open_tabs)}")
        return "  ".join(bits)

    def for_prompt(self, want_selection: bool = False,
                   want_tabs: bool = True) -> dict[str, Any]:
        """The subset worth sending to a model.

        Selectively, not wholesale: dumping the whole machine costs tokens, buries the
        signal, and makes the model likelier to latch onto something irrelevant.
        """
        out: dict[str, Any] = {
            "frontmost_app": self.frontmost_app,
            "running_apps": self.running_apps,
        }
        if self.active_window:
            out["active_window"] = self.active_window
        if want_tabs and self.open_tabs:
            out["open_tabs"] = {f"t{i}": t[2][:110]
                                for i, t in enumerate(self.open_tabs[:MAX_TABS_IN_PROMPT])}
        if self.recent_turns:
            out["recent_turns"] = self.recent_turns
        if want_selection:
            sel = self.selected_text
            if sel:
                out["selected_text"] = sel
        if self.clipboard and want_selection:
            out["clipboard"] = self.clipboard
        return out


# Words that mean "the thing on screen" rather than naming it. When one of these is
# present the request cannot be resolved from the sentence alone, so it is worth
# paying for the intrusive context.
DEICTIC = (
    "this", "that", "it", "these", "those", "him", "her", "them", "they",
    "here", "there", "the selection", "selected", "highlighted",
    "current", "this one", "that one",
)


def needs_context(utterance: str) -> bool:
    """Does resolving this require looking at the screen?"""
    low = f" {utterance.lower()} "
    return any(f" {w} " in low for w in DEICTIC)
