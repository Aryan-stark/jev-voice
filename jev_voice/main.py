"""Jev Voice: speak to your Mac.

    uv run jev-voice                     # hands-free: say "Alfred, ..." (or tap Caps Lock, then speak)
    uv run jev-voice --hold              # Caps Lock: hold to talk (tap = toggle), no wake word
    uv run jev-voice --always-on         # open mic, every utterance is a command (no wake word)
    uv run jev-voice --ptt               # press Enter to talk, Enter to stop
    uv run jev-voice --text "open chrome and go to youtube"      # no mic
    uv run jev-voice --text "..." --dry-run                        # plan only
"""
from __future__ import annotations

import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time

import numpy as np

from . import actions, config
from .brain import Brain, Plan, split_compound
from .overlay import NullOverlay
from .context import Context
from . import context
from .runtime import Runtime
from .telemetry import Recorder
from .tools import Risk
from . import tools
from .macros import run as run_macro
from .persona import flavor
from .tts import Speaker

OVERLAY = NullOverlay()  # replaced with a real Overlay in run_voice unless --no-overlay

SOUND_START = "/System/Library/Sounds/Tink.aiff"
SOUND_STOP = "/System/Library/Sounds/Pop.aiff"
SOUND_FAIL = "/System/Library/Sounds/Basso.aiff"
SOUND_DONE = "/System/Library/Sounds/Glass.aiff"
# FEEDBACK=ding (default): chime when an action completes, no speech. FEEDBACK=voice: spoken butler replies.
FEEDBACK = os.environ.get("FEEDBACK", "ding")
IDLE_LABEL = "Listening"


def ding(path: str) -> None:
    subprocess.Popen(["afplay", "-v", "0.4", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def execute(plan: Plan, dry: bool = False, rt: Runtime | None = None) -> str:
    """Run the plan. Returns the short spoken confirmation."""
    a = plan.args
    act = plan.action
    rt = rt or Runtime()
    if act == "none":
        return ""
    if act == "stop":
        return "__stop__"
    if plan.confidence < config.ACTION_MIN_CONFIDENCE:
        return "Not sure what you meant."
    if dry:
        return f"[dry] {plan}"
    if a.get("in_app"):
        if not actions.focus_app(a["in_app"]):
            return f"I couldn't bring up {a['in_app']}."
    if act == "new_item":
        actions.press({"tab": "new_tab", "window": "new"}.get(a.get("kind", "item"), "new"))
        title = a.get("title")
        if title:
            time.sleep(0.35)
            actions.type_text(title)
            return f"New {title}."
        return "Done."
    if act == "open_app":
        if a["app"] == "none":
            return "I don't see that app."
        # Verify rather than assume: `open -a` succeeding does not mean the app appeared.
        if not actions.open_app(a["app"], verify=2.5):
            return f"I couldn't open {a['app']}."
        return f"Opening {a['app']}."
    if act == "open_website":
        actions.open_url(a["url"])
        return f"Opening {a['site'].replace('_', ' ')}."
    if act == "web_search":
        actions.web_search(a["engine"], a["query"])
        return f"Searching {a['engine'].replace('_', ' ')} for {a['query']}."
    if act == "type_text":
        actions.type_text(a["text"])
        if a["submit"]:
            actions.press("enter")
        return "Done."
    if act == "shortcut":
        actions.press(a["shortcut"])
        return a["shortcut"].replace("_", " ").capitalize() + "."
    if act == "scroll":
        actions.scroll(a["direction"], a["amount"])
        return ""
    if act == "volume":
        try:
            prev = actions.get_volume()
            rt.push_undo("the volume change", lambda: actions.set_volume(prev))
        except Exception:  # noqa: BLE001
            pass
        return actions.volume(a["op"])
    if act == "media":
        actions.media(a["op"])
        return ""
    if act == "screenshot":
        actions.screenshot()
        return "Screenshot saved to the desktop."
    if act == "open_folder":
        actions.open_folder(a["folder"])
        return f"Opening {a['folder']}."
    if act == "system":
        if a["op"] == "toggle_dark_mode":
            rt.push_undo("the appearance change",
                         lambda: actions.system("toggle_dark_mode"))
        return actions.system(a["op"])
    if act == "run_macro":
        name = a.get("macro", "none")
        if name == "none":
            return "I don't have a routine for that."
        ok, summary = run_macro(name, dry)
        print(f"  ⚙ {summary}")
        return summary if ok else f"I couldn't finish that: {summary}"
    if act == "take_note":
        text = a.get("text", "").strip()
        if not text:
            return "There was nothing to note."
        if a.get("kind") == "reminder":
            return actions.reminder_capture(text)
        reply, _nid = actions.note_capture(text)
        # No undo entry: Notes cannot delete or edit a note via AppleScript, so an
        # "undo" here would report success and change nothing.
        return reply
    if act == "switch_tab":
        if "win" not in a:
            return "I don't see that tab."
        if not actions.activate_tab(a["win"], a["tab"]):
            return "I couldn't switch to that tab."
        return f"{a.get('title', 'That tab')[:40]}."
    if act == "close_named_tab":
        if "win" not in a:
            return "I don't see that tab."
        title = a.get("title", "")
        url = actions.tab_url(a["win"], a["tab"])   # capture before closing, so undo can reopen
        if not actions.close_tab(a["win"], a["tab"]):
            return "I couldn't close that tab."
        if url:
            rt.push_undo("closing that tab", lambda: actions.open_in_new_tab(url))
        return f"Closed {title[:40]}."
    if act == "start_dictation":
        if os.environ.get("JEV_MODE") == "always-on":
            return "Dictation needs hold-to-talk or hands-free mode."
        rt.start_dictation()
        return "Dictating."
    if act == "stop_dictation":
        rt.stop_dictation()
        return "Done dictating."
    if act == "undo_last":
        u = rt.pop_undo()
        if u is None:
            return "There's nothing to undo."
        try:
            u.revert()
        except Exception:  # noqa: BLE001
            return f"I couldn't undo {u.label}."
        return f"Undid {u.label}."
    return ""


_STOP_DICT = re.compile(
    r"\b(?:stop|end|finish|cancel|quit)\s+(?:the\s+)?dictat(?:ing|ion)\b"
    r"|\bthat'?s (?:the end|it) (?:of|for) (?:the )?dictation\b", re.I)


def handle(brain: Brain, speaker: Speaker, utterance: str, dry: bool, depth: int = 0,
           plan: Plan | None = None, rt: Runtime | None = None,
           rec: "Recorder | None" = None) -> bool:
    """Returns False when the user asked to stop."""
    rt = rt or Runtime()
    # Sub-steps of a compound command share the parent's record.
    own_record = rec is None and depth == 0
    if own_record:
        rec = Recorder(mode="text")
    if rec is not None and depth == 0:
        rec.set(transcript=utterance)

    # A held action is answered locally: "yes"/"no" must never cost an API round-trip,
    # and must never be re-interpreted as a fresh command.
    if rt.pending is not None and depth == 0 and plan is None:
        if rt.pending.expired():
            rt.pending = None
        elif _NO.match(utterance):
            held = rt.take_pending()
            reply = "Cancelled."
            print(f"  ◀ {reply} (was: {held.description if held else ''})")
            speak_reply(speaker, reply)
            OVERLAY.set("idle", "Cancelled", revert_after=2.0)
            if rec is not None:
                rec.set(action="cancelled", ok=True, reply=reply)
                if own_record:
                    rec.write()
            return True
        elif _YES.match(utterance):
            held = rt.take_pending()
            if held is None:
                reply = "That's expired -- say it again."
            else:
                print(f"  ⚙ confirmed: {held.description}")
                ok, reply = held.run()
            speak_reply(speaker, reply)
            OVERLAY.set("done", reply[:70], revert_after=3.0)
            if rec is not None:
                rec.set(action="confirmed", ok=True, reply=reply)
                if own_record:
                    rec.write()
            return True

    # Dictation short-circuits the model entirely: no API call, and no chance that
    # dictated words like "open chrome" get executed as a command.
    if rt.dictating and depth == 0 and plan is None:
        if _STOP_DICT.search(utterance):
            rt.stop_dictation()
            print("  ✍ dictation off")
            OVERLAY.set("done", "Dictation off", revert_after=2.0)
            if FEEDBACK == "voice":
                speaker.say(flavor("Done dictating."))
            else:
                ding(SOUND_STOP)
            return True
        if rt.dictation_expired():
            rt.stop_dictation()
            print("  ✍ dictation timed out")
        else:
            print(f"  ✍ {utterance}")
            if not dry:
                actions.type_text(utterance + " ")
            rt.touch_dictation()
            OVERLAY.set("done", f"✍ {utterance}", revert_after=1.5)
            return True

    OVERLAY.set("thinking", f"{utterance}")
    plan = plan or brain.evaluate(utterance, rt=rt)
    print(f"  → {plan}")
    if rec is not None and depth == 0:
        rec.mark("jev", plan.latency_ms)
        rec.set(action=plan.action, confidence=round(plan.confidence, 3))

    # Jev must pick one of its ~20 actions, so an out-of-vocabulary request comes back
    # either as `none` or as a low-confidence nearest match. In both cases guessing is
    # worse than thinking, so hand it to the planner, which is allowed to refuse.
    if depth == 0 and not dry and _should_escalate(plan):
        from . import planner

        if planner.available():
            print(f"  ↗ escalating to {planner.model_name()}…")
            OVERLAY.set("thinking", "Working it out…")
            # The planner gets the machine's state, so "send this to him" can resolve
            # what the sentence alone cannot. Context is lazy: a request with no
            # pointing words never pays for the intrusive parts.
            ctx = Context(rt=rt)
            if context.needs_context(utterance):
                print(f"  👁 {ctx.summary()}")
            if rec is not None:
                rec.set(escalated=True)
                with rec.stage("planner"):
                    steps, refusal = planner.plan(utterance, ctx=ctx)
            else:
                steps, refusal = planner.plan(utterance, ctx=ctx)
            if refusal:
                reply = f"I can't do that: {refusal}"
                print(f"  ◀ {reply}")
                speak_reply(speaker, reply)
                OVERLAY.set("error", reply[:70], revert_after=4.0)
                rt.record(utterance, "refused", reply, False)
                if rec is not None:
                    rec.set(refused=True, ok=True, reply=reply)
                    if own_record:
                        rec.write()
                return True
            print(f"  ⚙ plan: {[s.get('do') for s in steps]}")
            if rec is not None:
                rec.set(plan_steps=[str(s.get("do")) for s in steps])

            # A model-authored plan doesn't get to do something destructive unasked.
            level, step_name = riskiest(steps)
            if level > CONFIRM_ABOVE:
                rt.hold(describe_steps(steps),
                        lambda st=steps, g=utterance: run_steps(st, goal=g), level.name)
                reply = (f"That would {step_name.replace('_', ' ')} "
                         f"({level.name.lower()} risk). Say yes to go ahead.")
                print(f"  ⚠ held: {level.name} via {step_name}")
                print(f"  ◀ {reply}")
                speak_reply(speaker, reply)
                OVERLAY.set("error", reply[:70], revert_after=6.0)
                rt.record(utterance, "held", reply, True)
                if rec is not None:
                    rec.set(ok=True, reply=reply, action="held")
                    if own_record:
                        rec.write()
                return True

            if rec is not None:
                with rec.stage("execute"):
                    ok, summary = run_steps(steps, goal=utterance)
            else:
                ok, summary = run_steps(steps, goal=utterance)
            print(f"  ⚙ {summary}")
            speak_reply(speaker, summary if ok else f"That didn't finish: {summary}")
            OVERLAY.set("done" if ok else "error", summary[:70], revert_after=3.0)
            rt.record(utterance, "planned", summary, ok)
            if rec is not None:
                rec.set(ok=ok, reply=summary, error="" if ok else summary)
                if own_record:
                    rec.write()
            return True

    if plan.args.get("compound") and depth == 0:
        parts = split_compound(utterance)
        if len(parts) > 1:
            print(f"  compound: {parts}")
            if rec is not None:
                rec.set(plan_steps=parts)
            for p in parts:
                if not handle(brain, speaker, p, dry, depth=1, rt=rt, rec=rec):
                    return False
                time.sleep(0.35)  # let the previous app/page come up
            if rec is not None:
                rec.set(ok=True, reply=f"compound: {len(parts)} parts")
                if own_record:
                    rec.write()
            return True
    if plan.action == "run_macro" and plan.confidence >= config.ACTION_MIN_CONFIDENCE:
        ack = flavor("On it.")
        print(f"  ◀ {ack}")
        OVERLAY.set("thinking", f"{describe(plan)}…")
        if FEEDBACK == "voice":
            speaker.say(ack)
    # Same gate as the planner path: a confident classification is not a licence to
    # destroy something. Jev reaching `empty_trash` at 1.00 is exactly the dangerous case.
    if depth == 0 and not dry:
        level, what = plan_risk(plan)
        if level > CONFIRM_ABOVE:
            rt.hold(describe(plan), lambda p=plan: (True, execute(p, False, rt=rt)),
                    level.name)
            reply = (f"That would {describe(plan).lower()} "
                     f"({level.name.lower()} risk). Say yes to go ahead.")
            print(f"  ⚠ held: {level.name} via {what}")
            print(f"  ◀ {reply}")
            speak_reply(speaker, reply)
            OVERLAY.set("error", reply[:70], revert_after=6.0)
            if rec is not None:
                rec.set(action="held", ok=True, reply=reply)
                if own_record:
                    rec.write()
            return True
    try:
        if rec is not None:
            with rec.stage("execute"):
                reply = execute(plan, dry, rt=rt)
        else:
            reply = execute(plan, dry, rt=rt)
    except Exception as e:  # noqa: BLE001
        reply = "That failed."
        print(f"  ! {e}")
        if rec is not None:
            rec.set(error=f"{type(e).__name__}: {e}")
    if reply == "__stop__":
        if rec is not None:
            rec.set(ok=True, reply="stop")
            if own_record:
                rec.write()
        if FEEDBACK == "voice":
            speaker.say(flavor("Bye."))
        else:
            ding(SOUND_STOP)
        return False
    failed = (reply in ("That failed.", "Not sure what you meant.", "I don't see that app.",
                        "I don't see that tab.", "There's nothing to undo.",
                        "There was nothing to note.")
              or reply.startswith("I couldn't"))
    if depth == 0 and plan.action != "none":
        rt.record(utterance, plan.action, reply, not failed)
    if rec is not None and depth == 0:
        rec.set(ok=not failed, reply=reply)
        if own_record:
            rec.write()
    if reply:
        line = flavor(reply) if not dry else reply
        print(f"  ◀ {line}")
        if FEEDBACK == "voice":
            speaker.say(line)
        elif failed:
            ding(SOUND_FAIL)
        else:
            ding(SOUND_DONE)
    elif plan.action != "none" and not dry:
        ding(SOUND_DONE)
    if plan.action == "none":
        OVERLAY.set("idle", f"Not a command: {utterance}", revert_after=2.5)
    elif failed:
        OVERLAY.set("error", f"{describe(plan)}  ·  {reply}", revert_after=3.0)
    else:
        OVERLAY.set("done", describe(plan), revert_after=2.5)
    return True


ESCALATE_BELOW = float(os.environ.get("ESCALATE_BELOW_CONFIDENCE", "0.55"))
# A bad guess in these is destructive or outward-facing, so they escalate sooner.
_RISKY = {"system", "shortcut", "type_text"}


def _should_escalate(plan: Plan) -> bool:
    if plan.action in ("stop", "start_dictation", "stop_dictation", "undo_last"):
        return False
    if plan.action == "none":
        # Jev judged this wasn't aimed at the computer at all -- believe it, unless it
        # also thinks it was addressed, which is the "command I can't perform" case.
        return float(plan.args.get("addressed", 0)) >= 0.6
    if plan.action in _RISKY:
        return plan.confidence < max(ESCALATE_BELOW, 0.75)
    return plan.confidence < ESCALATE_BELOW


def speak_reply(speaker: Speaker, line: str) -> None:
    if FEEDBACK == "voice":
        speaker.say(flavor(line))
    else:
        ding(SOUND_DONE)


# Above this risk, a model-authored plan must be confirmed before it runs.
# MEDIUM and below execute automatically; HIGH and CRITICAL ask first.
CONFIRM_ABOVE = Risk[os.environ.get("CONFIRM_ABOVE_RISK", "MEDIUM").upper()]
_YES = re.compile(r"^\W*(?:yes|yep|yeah|yup|ok|okay|sure|do it|go ahead|go on|confirm|"
                  r"send it|send|proceed|please do)\b", re.I)
_NO = re.compile(r"^\W*(?:no|nope|don'?t|stop|cancel|never ?mind|forget it|abort|wait)\b", re.I)


# Jev's action vocabulary -> the registry tool whose risk applies. Actions absent here
# are either harmless or handled entirely inside execute().
_ACTION_TOOL = {
    "system": "system", "shortcut": "shortcut", "type_text": "type",
    "open_app": "open_app", "open_website": "open_url", "web_search": "web_search",
    "scroll": "scroll", "volume": "volume", "media": "media", "screenshot": "screenshot",
    "open_folder": "open_folder", "take_note": "note", "switch_tab": "switch_tab",
    "close_named_tab": "close_tab", "new_item": "shortcut",
}


def plan_risk(plan: Plan) -> tuple[Risk, str]:
    """Risk of one of Jev's own direct actions.

    This path is the common one -- Jev answers confidently and executes without ever
    consulting the planner -- so it needs the same gate a model-authored plan gets.
    """
    tool = _ACTION_TOOL.get(plan.action)
    if tool is None:
        return Risk.NONE, plan.action
    a = plan.args
    args: dict = {}
    if plan.action == "system":
        args = {"op": a.get("op", "")}
    elif plan.action in ("shortcut", "new_item"):
        args = {"key": a.get("shortcut", "")}
    return tools.risk_of(tool, args), plan.action


def riskiest(steps: list) -> tuple[Risk, str]:
    """The highest risk in a plan, and the step that carries it."""
    worst, worst_name = Risk.NONE, ""
    for st in steps:
        name = str(st.get("do") or "")
        r = tools.risk_of(name, {k: v for k, v in st.items() if k != "do"})
        if r > worst:
            worst, worst_name = r, name
    return worst, worst_name


def describe_steps(steps: list) -> str:
    return ", ".join(str(s.get("do")) for s in steps)


def run_steps(steps: list, goal: str = "") -> tuple[bool, str]:
    """Run planner-produced steps through the agent loop.

    The loop retries timing failures -- an app slow to front, a Slack switcher not yet
    ready -- which are the ones that actually recur here. It does not retry structural
    failures, and replanning is off by default; see agent.py for the measurements.
    """
    from . import agent
    from .macros import _step

    def step_fn(st: dict) -> tuple[bool, str]:
        try:
            ok, what = _step(st)
        except Exception as e:  # noqa: BLE001
            return False, f"{st.get('do')}: {e}"
        time.sleep(0.25)
        return ok, what

    replan_fn = None
    if agent.MAX_REPLANS > 0:
        from . import planner
        replan_fn = lambda g, rem, why: planner.replan(g, rem, why)  # noqa: E731

    out = agent.run(steps, step_fn, replan_fn=replan_fn,
                    on_event=lambda m: print(f"    {m}"), goal=goal)
    return out.ok, out.summary


def describe(plan: Plan) -> str:
    """Short human label for the overlay, e.g. 'Open Google Chrome'."""
    a = plan.args
    act = plan.action
    return {
        "open_app": lambda: f"Open {a.get('app')}",
        "open_website": lambda: f"Open {a.get('site', '').replace('_', ' ')}",
        "web_search": lambda: f"Search {a.get('engine', '').replace('_', ' ')}: {a.get('query')}",
        "type_text": lambda: f"Type “{a.get('text')}”" + (" ⏎" if a.get('submit') else ""),
        "new_item": lambda: f"New {a.get('kind', 'item')}" + (f" “{a['title']}”" if a.get('title') else ""),
        "shortcut": lambda: a.get("shortcut", "").replace("_", " ").capitalize(),
        "scroll": lambda: f"Scroll {a.get('direction')} ({a.get('amount')})",
        "volume": lambda: f"Volume {a.get('op')}",
        "media": lambda: f"Media {a.get('op', '').replace('_', '/')}",
        "screenshot": lambda: "Screenshot",
        "open_folder": lambda: f"Open {a.get('folder')}",
        "system": lambda: f"{a.get('op', '').replace('_', ' ').capitalize()}",
        "run_macro": lambda: f"Routine: {a.get('macro', '?').replace('_', ' ')}",
        "take_note": lambda: ("Reminder" if a.get("kind") == "reminder" else "Note")
                             + f": {a.get('text', '')[:50]}",
        "switch_tab": lambda: f"Tab: {a.get('title', '?')[:50]}",
        "close_named_tab": lambda: f"Close tab: {a.get('title', '?')[:50]}",
        "start_dictation": lambda: "Dictation on",
        "stop_dictation": lambda: "Dictation off",
        "undo_last": lambda: "Undo",
        "stop": lambda: "Bye",
    }.get(act, lambda: act)() + (f"  ·  in {a['in_app']}" if a.get("in_app") else "")


def run_text(args: argparse.Namespace) -> None:
    brain = Brain()
    speaker = Speaker(enabled=not args.quiet)
    handle(brain, speaker, args.text, args.dry_run, rt=Runtime())


class Session:
    """Shared runtime for all mic modes."""

    def __init__(self, args: argparse.Namespace) -> None:
        from .audio import Listener
        from .stt import WhisperServer

        if not actions.accessibility_ok():
            print("⚠ Accessibility permission missing: System Settings → Privacy & Security → Accessibility → add your terminal.")
        self.args = args
        self.mode = ("ptt" if args.ptt else "always-on" if args.always_on
                     else "hold" if args.hold else "smart")
        self.rt = Runtime()
        self.stt = WhisperServer()
        self.stt.start()
        self.brain = Brain()
        self.speaker = Speaker(enabled=not args.quiet)
        self.listener = Listener(device=args.device)
        self.listener.start()

    def close(self) -> None:
        self.listener.stop()
        self.stt.stop()

    def process(self, pcm: np.ndarray) -> bool:
        """Transcribe + plan + execute. Returns False on 'stop'."""
        if len(pcm) < config.SAMPLE_RATE * 0.25:
            return True
        secs = len(pcm) / config.SAMPLE_RATE
        rec = Recorder(mode=self.mode)
        rec.set(audio_seconds=round(secs, 2), wake_source="ptt")
        t0 = time.perf_counter()
        text = self.stt.transcribe(pcm)
        stt_ms = int((time.perf_counter() - t0) * 1000)
        rec.mark("stt", stt_ms)
        if not text:
            print("  (heard nothing)")
            # A blank transcript is a real signal: false endpoint, or silence.
            rec.set(transcript="", ok=None, reply="(heard nothing)")
            rec.write()
            return True
        print(f"🗣  {text}   ({secs:.1f}s audio, stt {stt_ms}ms)")
        self.listener.pause(0.3)
        ok = handle(self.brain, self.speaker, text, self.args.dry_run, rt=self.rt, rec=rec)
        rec.write()
        if self.speaker.speaking():
            self.listener.pause(0.9)
        self.listener.drain()
        return ok


# ------------------------------------------------------------------ wake word

WAKE_WORDS = [w.strip().lower() for w in os.environ.get("WAKE_WORDS", "alfred,jarvis,alfie,alford,elfred").split(",") if w.strip()]
FOLLOWUP_SECONDS = float(os.environ.get("FOLLOWUP_SECONDS", "8"))
_WAKE_RE = re.compile(r"^\W*(?:hey|hi|ok|okay|yo)?\W*(?P<w>" + "|".join(map(re.escape, WAKE_WORDS)) + r")\b\W*", re.I)
_WAKE_ANY = re.compile(r"\W*\b(?:" + "|".join(map(re.escape, WAKE_WORDS)) + r")\b\W*", re.I)


UNNAMED_COMMANDS = os.environ.get("UNNAMED_COMMANDS", "1") not in ("0", "false", "no")
UNNAMED_MIN_ADDRESSED = float(os.environ.get("UNNAMED_MIN_ADDRESSED", "0.7"))
UNNAMED_MIN_CONFIDENCE = float(os.environ.get("UNNAMED_MIN_CONFIDENCE", "0.7"))


def _fuzzy_wake(word: str) -> bool:
    import difflib

    w = word.lower().strip("',.!?;:")
    if len(w) < 4:
        return False
    for target in WAKE_WORDS:
        if w == target or difflib.SequenceMatcher(None, w, target).ratio() >= 0.75:
            return True
    return False


def strip_wake(text: str) -> tuple[bool, str]:
    """Return (addressed_to_us, command_text). The name may lead or appear anywhere,
    and whisper's misspellings of it (Alfrid, Halford, Alford's) count too."""
    m = _WAKE_RE.match(text)
    if m:
        return True, text[m.end():].strip()
    if _WAKE_ANY.search(text):
        return True, _WAKE_ANY.sub(" ", text, count=1).strip(" ,.")
    words = text.split()
    for i, w in enumerate(words):
        if _fuzzy_wake(w):
            rest = " ".join(words[:i] + words[i + 1:]).strip(" ,.")
            rest = re.sub(r"^(?:hey|hi|ok|okay|yo)\W+", "", rest, flags=re.I)
            return True, rest
    return False, text


# ------------------------------------------------------------------ modes

def ready(s: "Session") -> None:
    """Announce readiness the way FEEDBACK asks for."""
    if FEEDBACK == "voice":
        s.speaker.say(flavor("Ready."))
    else:
        ding(SOUND_DONE)


def run_smart(s: Session) -> None:
    """Hands-free. Mic is always open; only utterances that name the assistant (or follow
    a command within FOLLOWUP_SECONDS, or follow a Caps Lock tap) are sent to Jev."""
    from .hotkey import CapsLockListener, capslock_remapped, remap_capslock

    armed = {"until": 0.0}

    def arm(seconds: float) -> None:
        armed["until"] = time.monotonic() + seconds

    def on_press() -> None:
        s.speaker.interrupt()
        if s.rt.dictating:            # physical escape hatch out of dictation
            s.rt.stop_dictation()
            print("  ✍ dictation off (Caps Lock)")
            OVERLAY.set("done", "Dictation off", revert_after=2.0)
            ding(SOUND_STOP)
            return
        ding(SOUND_START)
        arm(10.0)

    if not capslock_remapped():
        remap_capslock()
    tap = CapsLockListener(on_press, lambda: None)
    caps = tap.start()
    names = ", ".join(w.capitalize() for w in WAKE_WORDS[:2])
    print(f"🎙  Hands-free. Say \"{names.split(', ')[0]}, open chrome\"."
          + (" Or tap CAPS LOCK then speak." if caps else " (Caps Lock tap unavailable: no Accessibility/Input Monitoring.)")
          + f"  (Jev {s.brain.model}, whisper base.en, voice {s.speaker.engine}:{s.speaker.voice})")
    if FEEDBACK == "voice":
        s.speaker.say(flavor("Ready."))
    else:
        ding(SOUND_DONE)
    s.listener.pause(0.8)
    s.listener.on_speech_start = lambda: OVERLAY.set("listening", "Listening…")
    while True:
        pcm = s.listener.next_utterance()
        OVERLAY.set("heard", "Transcribing…")
        rec = Recorder(mode="smart")
        rec.set(audio_seconds=round(len(pcm) / config.SAMPLE_RATE, 2))
        t0 = time.perf_counter()
        text = s.stt.transcribe(pcm)
        stt_ms = int((time.perf_counter() - t0) * 1000)
        rec.mark("stt", stt_ms)
        if not text:
            OVERLAY.set("idle", IDLE_LABEL, revert_after=0.1)
            rec.set(transcript="", reply="(heard nothing)")
            rec.write()
            continue
        OVERLAY.set("heard", text)
        rec.set(transcript=text)
        addressed, cmd = strip_wake(text)
        rec.set(addressed=addressed, wake_source="wake_word" if addressed else "none")
        if not addressed and time.monotonic() < armed["until"]:
            addressed, cmd = True, text
            rec.set(wake_source="armed")
        if not cmd and addressed:        # just the name: acknowledge and wait for the command
            ding(SOUND_START)
            arm(FOLLOWUP_SECONDS)
            rec.set(action="wake_only", ok=True, reply="(armed)")
            rec.write()
            continue
        gate = None
        if not addressed:
            if not UNNAMED_COMMANDS:
                print(f"   ·  {text}   (ignored: no name, stt {stt_ms}ms)")
                OVERLAY.set("idle", f"Ignored: {text}", revert_after=2.5)
                rec.set(action="ignored", reply="no wake word")
                rec.write()
                continue
            # No name: let Jev judge whether this is a command for the computer at all.
            gate = s.brain.evaluate(text, rt=s.rt)
            rec.mark("jev", gate.latency_ms)
            ok_cmd = (gate.args.get("addressed", 0) >= UNNAMED_MIN_ADDRESSED
                      and gate.confidence >= UNNAMED_MIN_CONFIDENCE)
            if ok_cmd and gate.action == "none":
                print(f"   ·  {text}   (Jev: none, addressed={gate.args.get('addressed')} conf={gate.confidence:.2f}, stt {stt_ms}ms)")
                rec.set(action="none", confidence=round(gate.confidence, 3), reply="(not a command)")
                rec.write()
                continue
            if not ok_cmd:
                print(f"   ·  {text}   (ignored: addressed={gate.args.get('addressed')} {gate.action} conf={gate.confidence:.2f}, stt {stt_ms}ms)")
                OVERLAY.set("idle", f"Ignored: {text}", revert_after=2.5)
                rec.set(action="ignored", confidence=round(gate.confidence, 3),
                        reply="below unnamed gate")
                rec.write()
                continue
            cmd = text
        tag = f", addressed={gate.args.get('addressed')}" if gate else ""
        print(f"🗣  {cmd}   ({len(pcm)/config.SAMPLE_RATE:.1f}s audio, stt {stt_ms}ms{tag})")
        s.listener.pause(0.3)
        ok = handle(s.brain, s.speaker, cmd, s.args.dry_run, plan=gate, rt=s.rt, rec=rec)
        rec.write()
        if s.speaker.speaking():
            s.listener.pause(0.9)
        s.listener.drain()
        arm(FOLLOWUP_SECONDS)
        if not ok:
            break


def run_capslock(s: Session) -> None:
    """Hold Caps Lock to talk, release to run. A short tap (<250 ms) toggles hands-free
    recording on; the next tap stops it."""
    from .hotkey import CapsLockListener, capslock_remapped, remap_capslock

    if not capslock_remapped():
        remap_capslock()
        if not capslock_remapped():
            print("⚠ Could not remap Caps Lock with hidutil. Run scripts/setup.sh.")
    recording = threading.Event()
    done: queue.Queue[np.ndarray] = queue.Queue()
    state = {"pressed_at": 0.0, "latched": False}

    def collector() -> None:
        while True:
            recording.wait()
            s.listener.drain()
            parts: list[np.ndarray] = []
            while recording.is_set():
                try:
                    parts.append(s.listener.q.get(timeout=0.05))
                except queue.Empty:
                    pass
            done.put(np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32))

    threading.Thread(target=collector, daemon=True).start()

    def on_press() -> None:
        state["pressed_at"] = time.monotonic()
        if state["latched"]:          # tap while latched: stop
            state["latched"] = False
            recording.clear()
            ding(SOUND_STOP)
            return
        s.speaker.interrupt()
        ding(SOUND_START)
        recording.set()

    def on_release() -> None:
        held = time.monotonic() - state["pressed_at"]
        if not recording.is_set():
            return
        if held < 0.25:               # short tap: latch hands-free
            state["latched"] = True
            return
        recording.clear()
        ding(SOUND_STOP)

    tap = CapsLockListener(on_press, on_release)
    if not tap.start():
        from .hotkey import open_permission_panes, request_permissions

        perms = request_permissions()
        print(f"✗ Global key tap refused. Permissions: {perms}")
        print("  Tick this app in System Settings → Privacy & Security → Accessibility AND Input Monitoring.")
        open_permission_panes()
        s.speaker.say("I need Accessibility and Input Monitoring permission. Please tick them in System Settings.")
        while True:
            time.sleep(2.0)
            tap = CapsLockListener(on_press, on_release)
            if tap.start():
                break
            if perms.get("accessibility") and perms.get("input_monitoring"):
                print("  Permissions granted but the tap still fails. Restart Jev Voice.")
                s.speaker.say("Permissions granted. Please restart me.")
                sys.exit(3)
            perms = request_permissions()
    print(f"⌨️  Hold CAPS LOCK and speak. Tap it to toggle hands-free. (Jev {s.brain.model}, whisper base.en, voice {s.speaker.engine}:{s.speaker.voice})")
    ready(s)
    while True:
        pcm = done.get()
        if not s.process(pcm):
            break


def run_always_on(s: Session) -> None:
    os.environ["JEV_MODE"] = "always-on"
    print(f"🎙  Listening (Jev {s.brain.model}, whisper base.en). Say 'stop listening' to quit.")
    ready(s)
    s.listener.pause(0.8)
    while True:
        pcm = s.listener.next_utterance()
        if not s.process(pcm):
            break


def run_ptt(s: Session) -> None:
    print("⏎  Push-to-talk: Enter to start, Enter to stop.")
    while True:
        input("  [Enter] to talk… ")
        s.listener.drain()
        print("  recording, [Enter] to stop")
        parts: list[np.ndarray] = []
        stop = threading.Event()

        def _collect() -> None:
            while not stop.is_set():
                try:
                    parts.append(s.listener.q.get(timeout=0.1))
                except queue.Empty:
                    pass

        th = threading.Thread(target=_collect, daemon=True)
        th.start()
        input()
        stop.set(); th.join()
        if parts and not s.process(np.concatenate(parts)):
            break


def run_voice(args: argparse.Namespace) -> None:
    global OVERLAY
    if not args.no_overlay and os.environ.get("OVERLAY", "1") not in ("0", "false", "no"):
        from .overlay import Overlay
        OVERLAY = Overlay()

    def worker() -> None:
        s = Session(args)
        try:
            if args.ptt:
                run_ptt(s)
            elif args.always_on:
                run_always_on(s)
            elif args.hold:
                run_capslock(s)
            else:
                run_smart(s)
        except KeyboardInterrupt:
            pass
        finally:
            s.close()

    OVERLAY.run(worker)


def main() -> None:
    p = argparse.ArgumentParser(prog="jev-voice", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--text", help="run one command from text instead of the microphone")
    p.add_argument("--dry-run", action="store_true", help="plan with Jev but do not touch the computer")
    p.add_argument("--hold", action="store_true", help="Caps Lock hold-to-talk only, no wake word")
    p.add_argument("--always-on", action="store_true", help="open mic, every utterance is a command (no wake word)")
    p.add_argument("--ptt", action="store_true", help="push-to-talk in the terminal (Enter to start/stop)")
    p.add_argument("--device", help="input device index or name substring (see `uv run python -m sounddevice`)")
    p.add_argument("--quiet", action="store_true", help="no spoken replies")
    p.add_argument("--no-overlay", action="store_true", help="no floating transcription pill")
    args = p.parse_args()
    if args.device and args.device.isdigit():
        args.device = int(args.device)
    if args.text:
        run_text(args)
    else:
        run_voice(args)


if __name__ == "__main__":
    sys.exit(main())
