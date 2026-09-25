"""Jev intent layer.

One HTTP request per utterance: a speculative fan-out of every question the
executor could need. Jev never generates text; every free-text value (text to
type, search query, domain) is produced as *candidates* in code and Jev selects
the right one (docs: "select instead of generate").
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from concurrent.futures import ThreadPoolExecutor

from . import actions, config, macros

# Tabs are a nice-to-have in state; never let them hold up a command.
TAB_TIMEOUT = float(__import__("os").environ.get("TAB_TIMEOUT", "0.5"))

ACTIONS: dict[str, str] = {
    "open_app": "Launch, open, switch to, or bring up an application program on the Mac (for example Chrome, Cursor, Slack, Finder, Terminal, Notes)",
    "open_website": "Go to a website or web page by name or domain, with no search query (for example 'go to youtube', 'open reddit', 'pull up gmail')",
    "web_search": "Search for something on the web or on a specific site: Google it, look it up, find videos of, search YouTube for, search Amazon for",
    "type_text": "Type, write, dictate, or enter some text into whatever is currently focused",
    "new_item": "Create something new inside an app: a new note, document, file, tab, window, message, email, or page (for example 'new note', 'open a new note in the notes app', 'make a new note called groceries', 'new document')",
    "shortcut": "Press a single key or keyboard shortcut: enter, escape, tab, copy, paste, undo, save, select all, new tab, close tab, reload, go back, quit the app, switch app, and similar",
    "scroll": "Scroll the current page or document up or down, to the top or bottom",
    "volume": "Change the system sound volume: louder, quieter, mute, unmute, max",
    "media": "Control music or video playback: play, pause, resume, next track, previous track, skip",
    "screenshot": "Take a screenshot of the screen",
    "open_folder": "Open a folder like Downloads, Desktop, Documents, or the home folder in Finder",
    "system": "System-level action: lock the screen, put the display to sleep, show the desktop, toggle dark mode, empty the trash",
    "take_note": "Capture a note, thought, reminder, or to-do item for later, without opening an app (for example 'take a note buy milk', 'note to self the wifi password is hunter2', 'add to my todo list pick up the parcel', 'remind me to send the invoice')",
    "switch_tab": "Switch to, go to, or bring up an already-open browser tab, named by what is in it (for example 'go to the claude tab', 'switch to the typesafe tab', 'the github one')",
    "close_named_tab": "Close a specific already-open browser tab named by its content (for example 'close the youtube tab', 'close the claude one')",
    "start_dictation": "Begin dictation mode: type out everything the user says, word for word, until they say stop (for example 'start dictating', 'take dictation', 'transcribe what I say')",
    "stop_dictation": "End dictation mode and go back to treating speech as commands (for example 'stop dictating', 'end dictation')",
    "undo_last": "Reverse an action the ASSISTANT itself just performed on the user's behalf, such as a note it created or a browser tab it closed ('undo that', 'take that back', 'never mind, revert that'). Plain 'undo' meaning the editing undo inside the focused app is `shortcut`, not this.",
    "run_macro": "Run one of the user's own saved multi-step routines by name (these are listed in the `macro` question; things like a morning setup routine, a focus mode, an end-of-day wrap-up)",
    "stop": "Tell the assistant to stop listening, go to sleep, or exit",
    "none": "Not a command for the computer: conversation, thinking aloud, background chatter, or unintelligible",
}

SHORTCUT_CRITERIA: dict[str, str] = {
    "enter": "press enter / return / submit",
    "escape": "press escape / cancel / dismiss",
    "tab": "press the tab key",
    "space": "press the space bar",
    "backspace": "delete the previous character / backspace",
    "arrow_up": "press the up arrow",
    "arrow_down": "press the down arrow",
    "arrow_left": "press the left arrow",
    "arrow_right": "press the right arrow",
    "copy": "copy the selection",
    "paste": "paste from the clipboard",
    "cut": "cut the selection",
    "undo": "undo the last change",
    "redo": "redo",
    "select_all": "select all / select everything",
    "save": "save the file / document",
    "find": "open find / search within the page or document",
    "new": "create a new file, document, note, or message in the current app",
    "new_tab": "open a new browser tab",
    "close_tab_or_window": "close the current tab or window",
    "reopen_closed_tab": "reopen the last closed tab",
    "quit_app": "quit / exit the current application entirely",
    "minimize_window": "minimize the window",
    "hide_app": "hide the current app",
    "fullscreen": "toggle full screen",
    "next_tab": "switch to the next tab",
    "previous_tab": "switch to the previous tab",
    "browser_back": "go back to the previous page",
    "browser_forward": "go forward",
    "reload": "reload / refresh the page",
    "address_bar": "focus the browser address bar / URL bar",
    "spotlight": "open Spotlight search",
    "switch_app": "switch to the previous / next application (command-tab)",
    "next_window": "switch to the next window of the current app",
    "delete_word": "delete the previous word",
    "delete_line": "delete the current line / everything before the cursor on this line",
    "zoom_in": "zoom in / make text bigger",
    "zoom_out": "zoom out / make text smaller",
    "bold": "make the selection bold",
    "italic": "make the selection italic",
    "send_message": "send the message (command-enter)",
    "emoji_picker": "open the emoji picker",
}

# regexes that peel the payload text off a spoken command
_TEXT_PATTERNS = [
    r"^(?:please\s+)?(?:can you\s+|could you\s+)?(?:type|write|enter|dictate|input|put|insert|say|send|text|paste)(?:\s+in|\s+out|\s+the\s+words?|\s+the\s+text|\s+this|\s+that)?[:,]?\s+(?P<t>.+)$",
    r"^(?:please\s+)?(?:can you\s+|could you\s+)?(?:search|google|look\s*up|find|look\s+for|show\s+me|pull\s+up)(?:\s+(?:on|in)\s+\w+(?:\s+\w+)?)?(?:\s+for)?[:,]?\s+(?P<t>.+)$",
    r"^.*?\b(?:for|about|of|on)\s+(?P<t>.+)$",
    r"[\"“'](?P<t>[^\"”']+)[\"”']",
]
_TITLE = re.compile(r"\b(?:called|titled|named|labeled|that says|saying|with the title)\s+(?P<t>.+)$", re.I)
_TRAILING_IN_APP = re.compile(r"\s+(?:in|into|inside|on)\s+(?:the\s+)?(?:[A-Z][\w.]*|notes|chrome|cursor|safari|slack|mail|messages|terminal|finder)(?:\s+app)?\s*[.!?]?$")
_TRAILING_SUBMIT = re.compile(
    r"[\s,.]*(?:and|then)?\s*(?:hit|press|and)\s+(?:enter|return|send|submit)\s*[.!]?$", re.I
)
_TODO_RE = re.compile(r"\b(?:to[\- ]?do|todo|task list|remind me|reminder|don'?t forget)\b", re.I)
_NOTE_LEAD = re.compile(
    r"^(?:please\s+)?(?:take\s+(?:a\s+)?note|note\s+(?:to\s+self|that|down)?|make\s+a\s+note"
    r"|jot\s+(?:this\s+)?down|remind\s+me\s+(?:to|that|about)|add\s+to\s+(?:my\s+)?"
    r"(?:to[\- ]?do|todo|task)\s*list)\b[:,]?\s*(?P<t>.+)$", re.I)
_SPLIT_COMPOUND = re.compile(r"\s*(?:,\s*)?\b(?:and then|then|and also|and)\b\s*", re.I)


def _clean(s: str) -> str:
    s = s.strip().strip('"“”\'')
    s = _TRAILING_SUBMIT.sub("", s).strip()
    return s.rstrip(" .").strip()


def text_candidates(utterance: str) -> dict[str, str]:
    """Candidate spans that might be the payload text. Jev picks; code never guesses."""
    cands: list[str] = []

    def add(t: str) -> None:
        t = _clean(t)
        if t and t not in cands:
            cands.append(t)

    m = _NOTE_LEAD.search(utterance)
    if m:
        add(m.group("t"))
    m = _TITLE.search(utterance)
    if m:
        add(m.group("t"))
    for pat in _TEXT_PATTERNS:
        m = re.search(pat, utterance, flags=re.I)
        if m:
            add(m.group("t"))
            add(_TRAILING_IN_APP.sub("", m.group("t")))
    whole = _clean(utterance)
    if whole and whole not in cands:
        cands.append(whole)
    if not cands:
        cands.append(utterance.strip() or "(nothing)")
    return {f"c{i}": c for i, c in enumerate(cands[:6])}


_DOMAIN = re.compile(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b", re.I)
_SITE_WORD = re.compile(
    r"\b(?:go to|open|visit|pull up|bring up|load|navigate to|take me to)\s+(?:the\s+)?(?:website\s+|site\s+)?([a-z0-9][a-z0-9 .-]*?)(?:\s+(?:website|site|dot com|\.com|page|homepage))?\s*[.!?]?$",
    re.I,
)


def domain_guess(utterance: str) -> str | None:
    m = _DOMAIN.search(utterance)
    if m:
        return m.group(1).lower()
    m = _SITE_WORD.search(utterance)
    if m:
        word = m.group(1).strip().lower().replace(" dot ", ".").replace(" ", "")
        if word and word not in ("it", "that", "this"):
            return word if "." in word else f"{word}.com"
    return None


@dataclass
class Plan:
    utterance: str
    action: str
    confidence: float
    args: dict[str, Any] = field(default_factory=dict)
    answers: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0

    def __str__(self) -> str:
        a = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
        return f"{self.action}({a})  conf={self.confidence:.2f}  {self.latency_ms}ms"


class Brain:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or config.TYPESAFE_API_KEY
        if not self.api_key:
            raise SystemExit("TYPESAFE_API_KEY is not set (put it in .env)")
        self.model = model or config.JEV_MODEL
        self.http = httpx.Client(
            timeout=15.0,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            http2=False,
        )
        # Keep the TCP+TLS connection warm so the first real command is fast.
        try:
            self.http.get(config.TYPESAFE_URL.rsplit("/", 1)[0] + "/models")
        except Exception:
            pass

    # ------------------------------------------------------------ questions

    def _questions(self, cands: dict[str, str], apps: list[str],
                   tabs: list[tuple[int, int, str]] | None = None,
                   can_undo: bool = False) -> dict[str, Any]:
        macro_opts = macros.criteria()
        acts = dict(ACTIONS)
        if not macro_opts:
            acts.pop("run_macro", None)
        else:
            acts["run_macro"] = (
                "Run one of the user's own saved multi-step routines. These are triggered "
                "by set phrases, including greetings and sign-offs that would otherwise "
                "look like small talk. The routines and the phrases that start them are: "
                + " | ".join(f"{n}: {d}" for n, d in macro_opts.items()))
            acts["none"] = (
                "Not a command for the computer: conversation, thinking aloud, background "
                "chatter, or unintelligible. Note that a greeting or sign-off that matches "
                "one of the saved routine phrases above is a command, not chatter.")
        if not can_undo:
            acts.pop("undo_last", None)
        if not tabs:
            acts.pop("switch_tab", None)
            acts.pop("close_named_tab", None)
        q: dict[str, Any] = {
            "action": {
                "type": "choice",
                "instructions": "The user is speaking a voice command to their Mac. `utterance` is the transcript. Which single kind of action are they asking the computer to perform right now?",
                "criteria": acts,
            },
            "addressed": {
                "type": "noul",
                "instructions": "Is `utterance` an instruction spoken to a voice assistant that controls this computer (open, type, search, scroll, press, play, close, and so on), rather than conversation with another person, a phone call, reading aloud, or thinking out loud?",
                "criteria": {"true": "A direct instruction for the computer to do something now", "false": "Not directed at the computer, or not an instruction"},
            },
            "compound": {
                "type": "noul",
                "instructions": "Does `utterance` ask for two or more separate actions to be performed one after another (for example 'open chrome and go to youtube')? A single action with several words is not compound.",
                "criteria": {"true": "Two or more distinct actions are requested", "false": "Exactly one action is requested"},
            },
            "app": {
                "type": "choice",
                "instructions": "Assume the user wants to open or switch to an application. Which installed application in `apps` do they mean? Match on meaning: 'chrome' means Google Chrome, 'settings' means System Settings, 'browser' means the default browser. Choose `none` if no listed app matches.",
                "criteria": {**{a: None for a in apps}, "none": "No listed application matches what the user said"},
            },
            "site": {
                "type": "choice",
                "instructions": "Assume the user wants to open a website. Which site do they mean? Choose `other` if it is not one of the listed sites.",
                "criteria": {**{s: None for s in actions.SITES}, "other": "A site not in this list"},
            },
            "engine": {
                "type": "choice",
                "instructions": "Assume the user wants to search for something. Which site or search engine should the search run on? If the user does not name a site, choose google.",
                "criteria": {e: None for e in actions.SEARCH_ENGINES},
            },
            "text": {
                "type": "choice",
                "instructions": "Assume the user wants some text typed or searched. `candidates` holds possible payloads cut from the utterance. Which candidate is exactly the payload text the user intends, with no command words (like 'type', 'search for', 'on youtube') and no trailing 'and press enter' included?",
                "criteria": {k: v for k, v in cands.items()},
            },
            "in_app": {
                "type": "noul",
                "instructions": "Does the user name a specific application that the action should happen inside of (for example 'in the notes app', 'in chrome', 'in cursor')? Naming an app as the thing to open does not count unless the action is something done inside it.",
                "criteria": {"true": "An application is named as the place where the action happens", "false": "No application is named, or the app is only the thing being opened"},
            },
            "new_kind": {
                "type": "choice",
                "instructions": "Assume the user wants to create something new. What kind of thing?",
                "criteria": {"tab": "a new browser tab", "window": "a new window", "item": "a new note, document, file, message, email, page, or anything else created with the app's New command"},
            },
            "has_title": {
                "type": "noul",
                "instructions": "Assume the user is creating a new note, document, or file. Do they give it a title or initial text (for example 'called groceries', 'titled ideas', 'that says hello')?",
                "criteria": {"true": "A title or initial text is given", "false": "No title or text is given"},
            },
            "submit": {
                "type": "noul",
                "instructions": "After typing the text, does the user also want the enter/return key pressed (they say things like 'and hit enter', 'and send it', 'and search')?",
                "criteria": {"true": "The user explicitly asks to submit, send, or press enter afterwards", "false": "They only want the text typed"},
            },
            "shortcut": {
                "type": "choice",
                "instructions": "Assume the user wants a key or keyboard shortcut pressed. Which one?",
                "criteria": SHORTCUT_CRITERIA,
            },
            "scroll_dir": {
                "type": "choice",
                "instructions": "Assume the user wants to scroll. In which direction?",
                "criteria": {"down": "scroll down / further", "up": "scroll up / back up", "top": "jump to the very top", "bottom": "jump to the very bottom"},
            },
            "scroll_amount": {
                "type": "choice",
                "instructions": "Assume the user wants to scroll up or down. How far?",
                "criteria": {"little": "a little / a bit / a few lines", "page": "a normal amount, about one screen; the default when unspecified", "a_lot": "a lot / way down / far"},
            },
            "volume_op": {
                "type": "choice",
                "instructions": "Assume the user wants to change the volume. What change?",
                "criteria": {"up": "louder / turn it up", "down": "quieter / turn it down", "mute": "mute / silence", "unmute": "unmute / sound back on", "max": "maximum / all the way up", "half": "medium / half volume"},
            },
            "media_op": {
                "type": "choice",
                "instructions": "Assume the user wants to control playback. What?",
                "criteria": {"play_pause": "play, pause, resume, or stop the current track or video", "next": "next track / skip this song", "previous": "previous track / go back a song"},
            },
            "folder": {
                "type": "choice",
                "instructions": "Assume the user wants to open a folder in Finder. Which one?",
                "criteria": {f: None for f in actions.FOLDERS},
            },
            "tab": {
                "type": "choice",
                "instructions": "Assume the user wants to switch to or close a browser tab that is already open. `tabs` lists the open tabs by title. Which tab do they mean? Match on meaning: 'the claude tab' means a tab whose title mentions Claude. Choose `none` if no open tab matches.",
                "criteria": {**{f"t{i}": t[2][:110] for i, t in enumerate(tabs or [])},
                             "none": "No open tab matches what the user said"},
            },
            "macro": {
                "type": "choice",
                "instructions": "Assume the user wants to run one of their own saved routines. Which one do they mean?",
                "criteria": {**macro_opts, "none": "None of these routines matches"},
            },
            "system_op": {
                "type": "choice",
                "instructions": "Assume the user wants a system-level action. Which one?",
                "criteria": {"lock": "lock the screen", "sleep_display": "put the display / screen to sleep", "show_desktop": "show the desktop", "toggle_dark_mode": "switch between dark and light mode", "empty_trash": "empty the trash"},
            },
        }
        if not tabs:
            q.pop("tab", None)
        if not macro_opts:
            q.pop("macro", None)
        return q

    # ------------------------------------------------------------ inference

    def evaluate(self, utterance: str, front: str | None = None,
                 rt: Any = None) -> Plan:
        # Enumerating tabs costs an AppleScript round-trip (~100-150ms), so overlap it
        # with the local work instead of paying for it serially.
        tabs: list[tuple[int, int, str]] = []
        pool: Any = None
        if actions.current_browser():
            pool = ThreadPoolExecutor(max_workers=1)
            fut = pool.submit(actions.browser_tabs)
        apps = actions.installed_apps()
        cands = text_candidates(utterance)
        frontmost = front if front is not None else actions.frontmost_app()
        running = actions.running_apps()
        if pool is not None:
            try:
                tabs = fut.result(timeout=TAB_TIMEOUT)
            except Exception:  # noqa: BLE001
                tabs = []
            pool.shutdown(wait=False)
        state = {
            "utterance": utterance,
            "frontmost_app": frontmost,
            "apps": apps,
            "running_apps": running,
            "candidates": cands,
        }
        if tabs:
            state["tabs"] = {f"t{i}": t[2][:110] for i, t in enumerate(tabs)}
        if rt is not None and rt.recent():
            # Gives pronouns an antecedent: "close it", "do that again", "undo that".
            state["recent_turns"] = rt.recent()
        can_undo = bool(rt is not None and rt.undo_stack)
        if can_undo:
            state["undoable"] = rt.undo_stack[-1].label
        payload = {"state": state, "model": self.model,
                   "questions": self._questions(cands, apps, tabs, can_undo)}
        t0 = time.perf_counter()
        r = self.http.post(config.TYPESAFE_URL, json=payload)
        r.raise_for_status()
        data = r.json()
        ms = int((time.perf_counter() - t0) * 1000)
        return self._to_plan(utterance, data["answers"], cands, ms, tabs)

    def _to_plan(self, utterance: str, ans: dict[str, Any], cands: dict[str, str], ms: int,
                 tabs: list[tuple[int, int, str]] | None = None) -> Plan:
        act = ans["action"]
        action = act["choice"]
        conf = float(act["confidence"])
        args: dict[str, Any] = {}

        def ch(key: str) -> tuple[str, float]:
            a = ans[key]
            return a["choice"], float(a["confidence"])

        if action == "open_app":
            app, c = ch("app")
            args["app"] = app
            conf = min(conf, c)
        elif action == "open_website":
            site, c = ch("site")
            if site != "other":
                args["url"] = actions.SITES[site]
                args["site"] = site
                conf = min(conf, c)
            else:
                dom = domain_guess(utterance)
                if dom:
                    args["url"] = f"https://{dom}"
                    args["site"] = dom
                else:  # nothing to navigate to: fall back to a search
                    action = "web_search"
        if action == "web_search":
            engine, c1 = ch("engine")
            tkey, c2 = ch("text")
            args["engine"] = engine
            args["query"] = cands.get(tkey, utterance)
            conf = min(conf, c2)
        elif action == "type_text":
            tkey, c = ch("text")
            args["text"] = cands.get(tkey, utterance)
            args["submit"] = float(ans["submit"]["noul"]) > config.YES
            conf = min(conf, c)
        elif action == "new_item":
            args["kind"], _ = ch("new_kind")
            if float(ans["has_title"]["noul"]) > config.YES:
                tkey, _ = ch("text")
                args["title"] = cands.get(tkey, "")
        elif action == "shortcut":
            s, c = ch("shortcut")
            args["shortcut"] = s
            conf = min(conf, c)
        elif action == "scroll":
            d, c = ch("scroll_dir")
            a, _ = ch("scroll_amount")
            args["direction"], args["amount"] = d, a
            conf = min(conf, c)
        elif action == "volume":
            op, c = ch("volume_op")
            args["op"] = op
            conf = min(conf, c)
        elif action == "media":
            op, c = ch("media_op")
            args["op"] = op
            conf = min(conf, c)
        elif action == "open_folder":
            f, c = ch("folder")
            args["folder"] = f
            conf = min(conf, c)
        elif action == "system":
            op, c = ch("system_op")
            args["op"] = op
            conf = min(conf, c)
        elif action == "run_macro":
            m, c = ch("macro") if "macro" in ans else ("none", 0.0)
            args["macro"] = m
            conf = min(conf, c)
        elif action == "take_note":
            tkey, _c = ch("text")
            args["text"] = cands.get(tkey, utterance)
            args["kind"] = "reminder" if _TODO_RE.search(utterance) else "note"
            # Deliberately not min()'d with the span confidence: the candidates are
            # near-duplicates ("send the invoice" vs "remind me to send the invoice")
            # and either makes an acceptable note, so splitting the vote between them
            # must not veto the action.
        elif action in ("switch_tab", "close_named_tab"):
            tkey, c = ch("tab") if "tab" in ans else ("none", 0.0)
            i = int(tkey[1:]) if tkey.startswith("t") and tkey[1:].isdigit() else -1
            if tabs and 0 <= i < len(tabs):
                args["win"], args["tab"], args["title"] = tabs[i]
            conf = min(conf, c)

        # "in the notes app": focus that app before acting inside it
        if action in ("new_item", "shortcut", "type_text", "scroll") and float(ans["in_app"]["noul"]) > config.YES:
            app, _ = ch("app")
            if app != "none":
                args["in_app"] = app
        args["compound"] = float(ans["compound"]["noul"]) > config.YES
        args["addressed"] = round(float(ans["addressed"]["noul"]), 2)
        return Plan(utterance, action, conf, args, ans, ms)


_SUBMIT_ONLY = re.compile(r"^(?:then\s+)?(?:hit|press|and)?\s*(?:enter|return|send|submit)(?:\s+it)?$", re.I)


def split_compound(utterance: str) -> list[str]:
    """Split 'A and then B' into parts. A trailing 'hit enter' is not its own step:
    the type_text plan already carries submit=True."""
    parts = [p.strip(" ,.") for p in _SPLIT_COMPOUND.split(utterance)]
    parts = [p for p in parts if len(p) > 1]
    if len(parts) > 1 and _SUBMIT_ONLY.match(parts[-1]):
        parts = parts[:-1]
        if len(parts) == 1:
            return [utterance]
        parts[-1] = parts[-1] + " and hit enter"
    return parts
