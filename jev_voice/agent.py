"""Act → observe → verify → repair.

The runner this replaces stopped at the first step it could not confirm. That is safe
but not useful: a plan that fails on step 1 of 4 leaves the machine half-configured and
the user re-dictating.

The design follows what the GUI-agent literature converged on (PAOVR; VeriGUI's
expectation-before-action), with one correction from measurement on this system:

    Retrying is almost never the answer here.

Of the failures this system actually produces -- app not installed, no such tab, unknown
shortcut, wrong Slack channel -- none are transient, so a retry loop burns time and
changes nothing. Only timing failures (an app slow to front, a switcher not yet ready)
repay a retry. Everything else needs a *different plan*, informed by why the last one
failed. So failures are classified before they are handled.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from . import tools

MAX_RETRIES = int(os.environ.get("AGENT_MAX_RETRIES", "2"))
# Replanning is OFF by default, on measured evidence rather than principle. Against the
# real failures this system produces it repaired 1 of 4 -- and that one "repair" turned
# "open Notion" (not installed) into a web search for "how to open Notion on Mac", which
# it then reported as success. A plausible-looking wrong action that claims ok is worse
# than a clean failure, and the other three cost 1.4-4.4s to conclude nothing.
# Set AGENT_MAX_REPLANS=1 to opt back in.
MAX_REPLANS = int(os.environ.get("AGENT_MAX_REPLANS", "0"))
RETRY_BACKOFF = 0.4


class Failure(Enum):
    TRANSIENT = "transient"      # a race or a slow app: the same step may work if repeated
    STRUCTURAL = "structural"    # the step is wrong: a different plan is needed
    FATAL = "fatal"              # nothing here will work; stop and say so


# Matched against a step's failure reason. Ordered: first match wins.
_TRANSIENT = re.compile(
    r"wouldn'?t come to the front|didn'?t open|timed out|not yet|still loading"
    r"|didn'?t land|not ready|temporarily", re.I)
_FATAL = re.compile(
    r"no Accessibility permission|unknown tool|not allowed to send keystrokes", re.I)


def classify(reason: str) -> Failure:
    if _FATAL.search(reason):
        return Failure.FATAL
    if _TRANSIENT.search(reason):
        return Failure.TRANSIENT
    return Failure.STRUCTURAL


@dataclass
class StepResult:
    step: dict[str, Any]
    ok: bool
    detail: str
    attempts: int = 1
    kind: Failure | None = None


@dataclass
class Outcome:
    ok: bool
    summary: str
    results: list[StepResult] = field(default_factory=list)
    replans: int = 0

    @property
    def completed(self) -> int:
        return sum(1 for r in self.results if r.ok)


def _expectation(step: dict[str, Any]) -> str:
    """What should be true after this step.

    Declaring it before acting is what gives the verify stage teeth -- otherwise
    "it didn't raise" silently becomes the definition of success.
    """
    t = tools.get(str(step.get("do")))
    if t is None:
        return "the step runs"
    target = step.get("app") or step.get("title") or step.get("to") or step.get("op") or ""
    return f"{t.name}{f' → {target}' if target else ''}"


def run(steps: list[dict[str, Any]],
        step_fn: Callable[[dict[str, Any]], tuple[bool, str]],
        replan_fn: Callable[[str, list[dict[str, Any]], str], list[dict[str, Any]] | None] | None = None,
        on_event: Callable[[str], None] | None = None,
        goal: str = "") -> Outcome:
    """Execute a plan, repairing what can be repaired.

    `step_fn` runs one step. `replan_fn(goal, remaining, reason)` may return a revised
    tail of the plan; returning None means "cannot repair". Both are injected so this
    module stays testable without a model or a Mac.
    """
    say = on_event or (lambda _m: None)
    results: list[StepResult] = []
    replans = 0
    queue = list(steps)
    i = 0

    while queue:
        step = queue.pop(0)
        i += 1
        expect = _expectation(step)
        say(f"step {i}: {expect}")

        ok, detail, attempts, kind = False, "", 0, None
        for attempt in range(1, MAX_RETRIES + 2):
            attempts = attempt
            ok, detail = step_fn(step)
            if ok:
                break
            kind = classify(detail)
            if kind is not Failure.TRANSIENT or attempt > MAX_RETRIES:
                break
            say(f"  transient ({detail[:40]}) — retry {attempt}/{MAX_RETRIES}")
            time.sleep(RETRY_BACKOFF * attempt)

        results.append(StepResult(step, ok, detail, attempts, None if ok else kind))
        if ok:
            say(f"  ✓ {detail[:60]}")
            continue

        say(f"  ✗ {kind.value if kind else '?'}: {detail[:60]}")

        if kind is Failure.FATAL or replan_fn is None or replans >= MAX_REPLANS:
            return Outcome(False, _summary(False, results, replans, detail), results, replans)

        # Structural failure: the step was wrong, so ask for a different plan for the
        # part that is left, telling it exactly what went wrong.
        replans += 1
        say(f"  ↻ replanning ({replans}/{MAX_REPLANS})")
        revised = replan_fn(goal, [step] + queue, detail)
        if not revised:
            return Outcome(False, _summary(False, results, replans, detail), results, replans)
        queue = list(revised)

    return Outcome(True, _summary(True, results, replans, ""), results, replans)


def _summary(ok: bool, results: list[StepResult], replans: int, reason: str) -> str:
    done = sum(1 for r in results if r.ok)
    if ok:
        s = f"Done ({done} step{'s' if done != 1 else ''}"
        return s + (f", replanned {replans}×)." if replans else ").")
    return (f"stopped after {done} of {len(results)} "
            f"({reason[:60]})" + (f" after {replans} replan" if replans else ""))
