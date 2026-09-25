"""Per-session mutable state.

Every Brain.evaluate() call is stateless, so anything that must survive across
utterances lives here: dictation mode, recent turns (so "do that again" and "close it"
have an antecedent), and the action journal that makes "undo that" possible.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

DICTATION_IDLE_TIMEOUT = float(os.environ.get("DICTATION_IDLE_SECONDS", "120"))
HISTORY_TURNS = 3


@dataclass
class Turn:
    utterance: str
    action: str
    result: str
    ok: bool


@dataclass
class Undo:
    """How to reverse one action. `label` is what we say when undoing."""
    label: str
    revert: Callable[[], Any]


CONFIRM_WINDOW = float(os.environ.get("CONFIRM_WINDOW_SECONDS", "25"))


@dataclass
class Pending:
    """An action held back because it was judged too risky to do unasked.

    Holds a thunk rather than steps, so the same mechanism covers both a planner-authored
    step list and one of Jev's own direct actions.
    """
    description: str
    run: Callable[[], tuple[bool, str]]
    deadline: float
    risk: str = ""

    def expired(self) -> bool:
        return time.monotonic() > self.deadline


@dataclass
class Runtime:
    dictating: bool = False
    dictation_until: float = 0.0
    history: list[Turn] = field(default_factory=list)
    undo_stack: list[Undo] = field(default_factory=list)
    pending: Pending | None = None

    # ---------------------------------------------------------- confirmation

    def hold(self, description: str, run: Callable[[], tuple[bool, str]],
             risk: str = "") -> None:
        self.pending = Pending(description, run, time.monotonic() + CONFIRM_WINDOW, risk)

    def take_pending(self) -> Pending | None:
        """Consume the held action, unless the window has closed."""
        p, self.pending = self.pending, None
        return None if (p is None or p.expired()) else p

    # ---------------------------------------------------------- dictation

    def start_dictation(self) -> None:
        self.dictating = True
        self.touch_dictation()

    def touch_dictation(self) -> None:
        self.dictation_until = time.monotonic() + DICTATION_IDLE_TIMEOUT

    def dictation_expired(self) -> bool:
        return self.dictating and time.monotonic() > self.dictation_until

    def stop_dictation(self) -> None:
        self.dictating = False
        self.dictation_until = 0.0

    # ---------------------------------------------------------- history

    def record(self, utterance: str, action: str, result: str, ok: bool) -> None:
        self.history.append(Turn(utterance, action, result, ok))
        del self.history[:-HISTORY_TURNS]

    def recent(self) -> list[dict[str, Any]]:
        """Shaped for the model's `state`: what was asked, what ran, whether it worked."""
        return [{"said": t.utterance, "did": t.action, "outcome": t.result, "worked": t.ok}
                for t in self.history]

    def last_action(self) -> str | None:
        return self.history[-1].action if self.history else None

    # ---------------------------------------------------------- undo

    def push_undo(self, label: str, revert: Callable[[], Any]) -> None:
        self.undo_stack.append(Undo(label, revert))
        del self.undo_stack[:-10]

    def pop_undo(self) -> Undo | None:
        return self.undo_stack.pop() if self.undo_stack else None
