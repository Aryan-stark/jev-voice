"""macOS execution layer. Everything here is plain code: no model involved."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

# ---------------------------------------------------------------- apps

APP_DIRS = [
    Path("/Applications"),
    Path("/Applications/Utilities"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    Path.home() / "Applications",
]

# Apps that are almost always worth having in the choice set even if not installed
# in /Applications (they live in system locations or are aliases people say aloud).
ALWAYS_APPS = ["Finder", "Safari", "Terminal", "System Settings", "Notes", "Messages",
               "Mail", "Calendar", "Music", "Reminders", "Photos", "Calculator",
               "TextEdit", "Preview", "Activity Monitor", "FaceTime", "Maps"]


# The API caps a Choice at 255 options; leave one slot for "none".
MAX_APP_CHOICES = 254


@lru_cache(maxsize=1)
def installed_apps() -> list[str]:
    names: set[str] = set(ALWAYS_APPS)
    for d in APP_DIRS:
        if not d.exists():
            continue
        for p in d.iterdir():
            if p.suffix == ".app":
                names.add(p.stem)
    ordered = sorted(names, key=str.lower)
    if len(ordered) > MAX_APP_CHOICES:  # a Choice accepts at most 255 options
        keep = [a for a in ordered if a in set(ALWAYS_APPS)]
        rest = [a for a in ordered if a not in set(ALWAYS_APPS)]
        ordered = sorted(keep + rest[: MAX_APP_CHOICES - len(keep)], key=str.lower)
    return ordered


def running_apps() -> list[str]:
    """Apps with a UI that are open right now. Free: no permission needed, unlike
    window titles (Screen Recording) or the AX tree (Accessibility)."""
    try:
        from AppKit import NSWorkspace  # type: ignore

        return sorted(
            {str(a.localizedName()) for a in NSWorkspace.sharedWorkspace().runningApplications()
             if a.activationPolicy() == 0 and a.localizedName()}
        )
    except Exception:
        return []


def frontmost_app() -> str:
    """The app that currently has focus.

    Must use CGWindowList. Every NSWorkspace route -- frontmostApplication(),
    isActive(), menuBarOwningApplication() -- caches after its first call in a process
    with no running run loop, so they report whatever was frontmost at startup for the
    life of the process. CGWindowList queries the window server directly and updates
    immediately; the owner name needs no permission (only window *titles* do).
    """
    try:
        import Quartz  # type: ignore

        wl = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID)
        for w in wl or []:
            if w.get("kCGWindowLayer") == 0 and w.get("kCGWindowOwnerName"):
                return str(w["kCGWindowOwnerName"])
    except Exception:
        pass
    try:
        from AppKit import NSWorkspace  # type: ignore

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName()) if app else ""
    except Exception:
        return ""


def open_app(name: str, verify: float = 0.0) -> bool:
    """Launch or activate an app. With verify>0, wait that long for it to actually appear
    and report whether it did, instead of assuming success."""
    out = subprocess.run(["open", "-a", name], capture_output=True, text=True)
    if out.returncode != 0:
        return False
    if verify <= 0:
        return True
    return wait_until(lambda: name.lower() in (a.lower() for a in running_apps()), verify)


def wait_until(pred, timeout: float, interval: float = 0.04) -> bool:
    """Poll pred() until true or timeout. Every wait in this module goes through here so
    there are no unconditional sleeps to tune."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def focus_app(name: str, timeout: float = 2.0) -> bool:
    """Open/activate an app and wait until it is frontmost (so keystrokes land in it)."""
    if frontmost_app().lower() == name.lower():
        return True
    open_app(name)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if frontmost_app().lower() == name.lower():
            time.sleep(0.15)  # let the window take key focus
            return True
        time.sleep(0.05)
    return False


def open_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- sites / search

SITES: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "google_calendar": "https://calendar.google.com",
    "google_drive": "https://drive.google.com",
    "google_docs": "https://docs.google.com",
    "google_maps": "https://maps.google.com",
    "github": "https://github.com",
    "twitter_x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "amazon": "https://www.amazon.com",
    "netflix": "https://www.netflix.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "notion": "https://www.notion.so",
    "spotify_web": "https://open.spotify.com",
    "linkedin": "https://www.linkedin.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "wikipedia": "https://en.wikipedia.org",
    "hacker_news": "https://news.ycombinator.com",
    "twitch": "https://www.twitch.tv",
    "figma": "https://www.figma.com",
    "typesafe_console": "https://console.typesafe.ai",
}

SEARCH_ENGINES: dict[str, str] = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "github": "https://github.com/search?q={q}",
    "google_maps": "https://www.google.com/maps/search/{q}",
    "twitter_x": "https://x.com/search?q={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "spotify": "https://open.spotify.com/search/{q}",
    "perplexity": "https://www.perplexity.ai/search?q={q}",
}


def web_search(engine: str, query: str) -> None:
    tpl = SEARCH_ENGINES.get(engine, SEARCH_ENGINES["google"])
    open_url(tpl.format(q=quote_plus(query)))


# ---------------------------------------------------------------- keyboard

OSASCRIPT_TIMEOUT = float(os.environ.get("OSASCRIPT_TIMEOUT", "5"))


def _osascript(script: str, timeout: float | None = None) -> str:
    """Run AppleScript. Always bounded: without Accessibility permission System Events
    blocks instead of failing, which would otherwise hang the assistant indefinitely."""
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                             timeout=timeout or OSASCRIPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("AppleScript timed out (is Accessibility permission granted?)") from None
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout.strip()


# ---------------------------------------------------------------- clipboard

PASTE_RESTORE_DELAY = float(os.environ.get("PASTE_RESTORE_DELAY", "0.6"))


def _pasteboard():
    from AppKit import NSPasteboard  # type: ignore

    return NSPasteboard.generalPasteboard()


def get_clipboard() -> str | None:
    from AppKit import NSPasteboardTypeString  # type: ignore

    try:
        return _pasteboard().stringForType_(NSPasteboardTypeString)
    except Exception:
        return None


def _pb_snapshot() -> tuple[int, list[dict]]:
    """Archive every flavor on the pasteboard plus the changeCount that produced it.
    The changeCount is what makes restoring safe."""
    pb = _pasteboard()
    items: list[dict] = []
    try:
        for it in (pb.pasteboardItems() or []):
            d = {}
            for t in (it.types() or []):
                data = it.dataForType_(t)
                if data is not None:
                    d[str(t)] = data
            if d:
                items.append(d)
    except Exception:
        pass
    return pb.changeCount(), items


def _pb_restore(expect_count: int, items: list[dict]) -> None:
    """Put the old clipboard back, but only if nothing else wrote in the meantime --
    otherwise a Cmd+C during the paste window would be silently clobbered."""
    from AppKit import NSPasteboardItem  # type: ignore

    pb = _pasteboard()
    try:
        if pb.changeCount() != expect_count:
            return
        pb.clearContents()
        objs = []
        for d in items:
            it = NSPasteboardItem.alloc().init()
            for t, data in d.items():
                it.setData_forType_(data, t)
            objs.append(it)
        if objs:
            pb.writeObjects_(objs)
    except Exception:
        pass


def set_clipboard(text: str) -> bool:
    """Write text and confirm it actually landed."""
    from AppKit import NSPasteboardTypeString  # type: ignore

    pb = _pasteboard()
    before = pb.changeCount()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)
    return wait_until(lambda: pb.changeCount() != before
                      and pb.stringForType_(NSPasteboardTypeString) == text, 0.15, 0.01)


def paste_text(text: str, restore: bool = True) -> bool:
    """Put text on the clipboard and press Cmd+V. Handles newlines, emoji and long text,
    all of which the keystroke path cannot.

    Note: macOS gives no way to confirm the paste landed in the target field -- only that
    the clipboard write succeeded. Callers that need certainty must read the field back.
    """
    import threading

    before_count, items = _pb_snapshot()
    if not set_clipboard(text):
        return False
    press("paste")
    if restore and items:
        ours = _pasteboard().changeCount()
        threading.Timer(PASTE_RESTORE_DELAY, _pb_restore, (ours, items)).start()
    return True


# ---------------------------------------------------------------- typing

# Long keystroke calls drop characters, especially in Electron apps.
KEYSTROKE_CHUNK = 200
# Above this length, or with any of these characters, pasting is more reliable.
PASTE_THRESHOLD = 40


def _keystroke(text: str) -> None:
    """Type literally. Newlines are sent as Return: a raw \n would break the
    AppleScript string literal."""
    for i, line in enumerate(text.split("\n")):
        if i:
            _osascript('tell application "System Events" to key code 36')
        for j in range(0, len(line), KEYSTROKE_CHUNK):
            chunk = line[j:j + KEYSTROKE_CHUNK]
            if not chunk:
                continue
            esc = chunk.replace("\\", "\\\\").replace('"', '\\"')
            _osascript(f'tell application "System Events" to keystroke "{esc}"')
            if j + KEYSTROKE_CHUNK < len(line):
                time.sleep(0.02)


def needs_paste(text: str) -> bool:
    return (len(text) > PASTE_THRESHOLD or "\n" in text
            or any(ord(c) > 0x2100 for c in text) or '"' in text or "\\" in text)


def type_text(text: str, method: str = "auto") -> None:
    """Enter text into the focused field. Pastes when that is more reliable, types
    otherwise, and falls back to typing if the clipboard route fails."""
    if not text:
        return
    if method == "keystroke" or (method == "auto" and not needs_paste(text)):
        _keystroke(text)
        return
    if not paste_text(text):
        _keystroke(text)


# name -> (key or key code, modifiers)
# Use "code:NN" for key codes, otherwise a literal character.
SHORTCUTS: dict[str, tuple[str, list[str]]] = {
    "enter": ("code:36", []),
    "escape": ("code:53", []),
    "tab": ("code:48", []),
    "space": ("code:49", []),
    "backspace": ("code:51", []),
    "delete_forward": ("code:117", []),
    "arrow_up": ("code:126", []),
    "arrow_down": ("code:125", []),
    "arrow_left": ("code:123", []),
    "arrow_right": ("code:124", []),
    "copy": ("c", ["command"]),
    "paste": ("v", ["command"]),
    "cut": ("x", ["command"]),
    "undo": ("z", ["command"]),
    "redo": ("z", ["command", "shift"]),
    "select_all": ("a", ["command"]),
    "save": ("s", ["command"]),
    "find": ("f", ["command"]),
    "new": ("n", ["command"]),
    "new_tab": ("t", ["command"]),
    "close_tab_or_window": ("w", ["command"]),
    "reopen_closed_tab": ("t", ["command", "shift"]),
    "quit_app": ("q", ["command"]),
    "minimize_window": ("m", ["command"]),
    "hide_app": ("h", ["command"]),
    "fullscreen": ("f", ["command", "control"]),
    "next_tab": ("code:48", ["control"]),
    "previous_tab": ("code:48", ["control", "shift"]),
    "browser_back": ("[", ["command"]),
    "browser_forward": ("]", ["command"]),
    "reload": ("r", ["command"]),
    "address_bar": ("l", ["command"]),
    "spotlight": ("code:49", ["command"]),
    "quick_switcher": ("k", ["command"]),
    "newline_in_message": ("code:36", ["shift"]),
    "switch_app": ("code:48", ["command"]),
    "next_window": ("`", ["command"]),
    "select_line_start": ("code:123", ["command", "shift"]),
    "select_line_end": ("code:124", ["command", "shift"]),
    "delete_word": ("code:51", ["option"]),
    "delete_line": ("code:51", ["command"]),
    "zoom_in": ("=", ["command"]),
    "zoom_out": ("-", ["command"]),
    "bold": ("b", ["command"]),
    "italic": ("i", ["command"]),
    "send_message": ("code:36", ["command"]),
    "screenshot_region": ("4", ["command", "shift"]),
    "emoji_picker": ("code:49", ["command", "control"]),
    "lock_screen": ("q", ["command", "control"]),
    "show_desktop": ("code:103", []),
}


def press(shortcut: str, times: int = 1) -> None:
    key, mods = SHORTCUTS[shortcut]
    using = ""
    if mods:
        using = " using {" + ", ".join(f"{m} down" for m in mods) + "}"
    if key.startswith("code:"):
        cmd = f"key code {key[5:]}{using}"
    else:
        cmd = f'keystroke "{key}"{using}'
    body = "\n".join([cmd] * max(1, times))
    _osascript(f'tell application "System Events"\n{body}\nend tell')


# ---------------------------------------------------------------- scroll

def scroll(direction: str, amount: str = "page") -> None:
    """direction: up|down|top|bottom ; amount: little|page|a_lot."""
    if direction in ("top", "bottom"):
        _osascript(
            'tell application "System Events" to key code %d using {command down}'
            % (126 if direction == "top" else 125)
        )
        return
    lines = {"little": 5, "page": 15, "a_lot": 40}.get(amount, 15)
    sign = 1 if direction == "up" else -1
    try:
        import Quartz  # type: ignore

        for _ in range(lines):
            ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, sign * 3)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.004)
    except Exception:
        press("arrow_up" if direction == "up" else "arrow_down", times=lines)


# ---------------------------------------------------------------- volume / media

def get_volume() -> int:
    return int(_osascript("output volume of (get volume settings)"))


def set_volume(level: int) -> None:
    level = max(0, min(100, level))
    _osascript(f"set volume output volume {level}")


def volume(op: str) -> str:
    if op == "mute":
        _osascript("set volume with output muted")
        return "Muted."
    if op == "unmute":
        _osascript("set volume without output muted")
        return "Unmuted."
    cur = get_volume()
    if op == "up":
        set_volume(cur + 15)
        return "Louder."
    if op == "down":
        set_volume(cur - 15)
        return "Quieter."
    if op == "max":
        set_volume(100)
        return "Max volume."
    if op == "half":
        set_volume(50)
        return "Half volume."
    return ""


_NX_KEYS = {"play_pause": 16, "next": 17, "previous": 18}


def media(op: str) -> None:
    """Post a HID media key event (works for Music, Spotify, YouTube in browsers)."""
    from AppKit import NSEvent  # type: ignore
    import Quartz  # type: ignore

    key = _NX_KEYS[op]
    for down in (True, False):
        flags = 0xA00 if down else 0xB00
        data1 = (key << 16) | ((0xA if down else 0xB) << 8)
        ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), flags, 0, 0, None, 8, data1, -1
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev.CGEvent())


# ---------------------------------------------------------------- misc

FOLDERS = {
    "home": Path.home(),
    "desktop": Path.home() / "Desktop",
    "downloads": Path.home() / "Downloads",
    "documents": Path.home() / "Documents",
    "pictures": Path.home() / "Pictures",
    "movies": Path.home() / "Movies",
    "applications": Path("/Applications"),
    "trash": Path.home() / ".Trash",
}


def open_folder(name: str) -> None:
    subprocess.Popen(["open", str(FOLDERS.get(name, Path.home()))])


def screenshot() -> Path:
    out = Path.home() / "Desktop" / f"Screenshot {time.strftime('%Y-%m-%d %H.%M.%S')}.png"
    subprocess.run(["screencapture", "-x", str(out)])
    return out


def system(op: str) -> str:
    if op == "lock":
        press("lock_screen")
        return "Locking."
    if op == "sleep_display":
        subprocess.Popen(["pmset", "displaysleepnow"])
        return "Sleeping the display."
    if op == "show_desktop":
        press("show_desktop")
        return "Showing desktop."
    if op == "toggle_dark_mode":
        _osascript('tell application "System Events" to tell appearance preferences to set dark mode to not dark mode')
        return "Toggled dark mode."
    if op == "empty_trash":
        _osascript('tell application "Finder" to empty trash')
        return "Emptied the trash."
    return ""


# ---------------------------------------------------------------- slack

SLACK_BLIND_SEND = os.environ.get("SLACK_BLIND_SEND", "0") not in ("0", "false", "no")


def _slack_title_matches(title: str, target: str) -> bool:
    """Slack's window title leads with the open conversation:
    'general (Channel) - Codeacious Tech - 7 new items - Slack'."""
    t = target.lstrip("#@").strip().lower()
    head = (title or "").split(" - ")[0].lower()
    head = re.sub(r"\s*\((channel|dm|direct message|group|private)\)\s*$", "", head).strip()
    if not t:
        return False
    if head == t:
        return True
    # A prefix match only counts at a word boundary, so "#general" never matches
    # "general-discussion" -- that would post to the wrong channel.
    return head.startswith(t) and not head[len(t):len(t) + 1].isalnum() \
        and head[len(t):len(t) + 1] not in ("-", "_")


def slack_send(target: str, message: str, send: bool = True) -> tuple[bool, str]:
    """Open a Slack conversation via the quick switcher and post a message.

    Slack has no scripting interface, so this is keystroke automation. Each step is
    confirmed through the Accessibility tree before the next runs, and the conversation
    is verified *before* any text is typed -- a fuzzy quick-switcher match must never
    put your message somewhere you didn't intend.

    Without Accessibility permission nothing is verifiable, so it refuses to send and
    leaves the message drafted (override with SLACK_BLIND_SEND=1).
    """
    from . import ax

    if not focus_app("Slack", timeout=6.0):
        return False, "Slack wouldn't come to the front"
    verifiable = ax.trusted()
    if not verifiable and not SLACK_BLIND_SEND:
        return False, "no Accessibility permission, so I can't verify where this would go"

    def switcher_open() -> bool:
        i = ax.focused_info("Slack")
        return i.get("role") in ("AXComboBox", "AXTextField") and "quer" in i.get("desc", "").lower()

    press("escape")
    time.sleep(0.2)
    press("quick_switcher")
    if verifiable and not ax.wait_until(switcher_open, 2.5):
        return False, "the quick switcher didn't open"

    if not paste_text(target, restore=True):
        return False, "couldn't put the name on the clipboard"
    needle = target.lstrip("#@").lower()
    if verifiable and not ax.wait_until(
            lambda: needle in (ax.focused_info("Slack").get("value") or "").lower(), 2.0):
        press("escape")
        return False, f"{target!r} didn't land in the switcher"

    time.sleep(0.5)          # let the result list re-rank before committing
    press("enter")

    if verifiable:
        # The decisive check: confirm the right conversation BEFORE typing anything.
        if not ax.wait_until(lambda: _slack_title_matches(ax.window_title("Slack"), target), 4.0):
            got = ax.window_title("Slack").split(" - ")[0]
            return False, f"opened {got!r}, not {target!r} - nothing was typed"

    if not paste_text(message, restore=True):
        return False, "couldn't put the message on the clipboard"
    time.sleep(0.25)
    if not send:
        return True, f"drafted in {target}"
    press("enter")
    return True, f"sent to {target}"


# ---------------------------------------------------------------- notes / reminders

def _as_str(s: str) -> str:
    """Escape for an AppleScript string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _as_html(s: str) -> str:
    """Notes bodies are HTML."""
    out = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return out.replace("\n", "<br>")


def note_capture(text: str) -> tuple[str, str]:
    """Create a note. Pure AppleScript: no window, no focus, no keystroke races."""
    title = text.split("\n", 1)[0][:60]
    body = _as_html(text)
    props = f'{{body:"{_as_str(body)}"}}'
    try:
        nid = _osascript(f'tell application "Notes" to tell account "iCloud" '
                         f'to return id of (make new note at folder "Notes" with properties {props})')
    except RuntimeError:
        # No iCloud account, or a differently-named default folder.
        nid = _osascript(f'tell application "Notes" to return id of '
                         f'(make new note with properties {props})')
    return f"Noted: {title[:40]}.", nid.strip()


# Notes is create-and-read-only over AppleScript: `delete`, `move` and `set body` all
# return success and do nothing (verified on macOS 26). So a created note cannot be
# taken back programmatically, and take_note deliberately registers no undo.


def reminder_capture(text: str, list_name: str | None = None) -> str:
    where = f' at list "{_as_str(list_name)}"' if list_name else ""
    try:
        _osascript(f'tell application "Reminders" to make new reminder{where} '
                   f'with properties {{name:"{_as_str(text)}"}}')
    except RuntimeError:
        _osascript(f'tell application "Reminders" to make new reminder '
                   f'with properties {{name:"{_as_str(text)}"}}')
    return f"Added to your list: {text[:40]}."


# ---------------------------------------------------------------- browser tabs

BROWSERS = ("Google Chrome", "Safari", "Brave Browser", "Microsoft Edge", "Arc")
MAX_TABS = 200


def current_browser() -> str | None:
    """The frontmost browser, else the first running one."""
    front = frontmost_app()
    if front in BROWSERS:
        return front
    running = set(running_apps())
    return next((b for b in BROWSERS if b in running), None)


def browser_tabs(app: str | None = None) -> list[tuple[int, int, str]]:
    """[(window_index, tab_index, title)] for the browser. A real closed option set, so
    Jev can select a tab rather than us guessing from the transcript."""
    app = app or current_browser()
    if not app:
        return []
    idx = "index of" if app == "Safari" else ""
    try:
        raw = _osascript(
            f'tell application "{_as_str(app)}"\n'
            f'set out to ""\n'
            f'repeat with w from 1 to count of windows\n'
            f'  repeat with t from 1 to count of tabs of window w\n'
            f'    set out to out & w & "|" & t & "|" & (title of tab t of window w) & linefeed\n'
            f'  end repeat\n'
            f'end repeat\n'
            f'return out\n'
            f'end tell', timeout=4.0)
    except RuntimeError:
        return []
    tabs: list[tuple[int, int, str]] = []
    for line in raw.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            tabs.append((int(parts[0]), int(parts[1]), parts[2].strip()))
    return tabs[:MAX_TABS]


def activate_tab(win: int, tab: int, app: str | None = None) -> bool:
    app = app or current_browser()
    if not app:
        return False
    try:
        if app == "Safari":
            _osascript(f'tell application "{_as_str(app)}" to set current tab of window {win} '
                       f'to tab {tab} of window {win}')
        else:
            _osascript(f'tell application "{_as_str(app)}" to set active tab index of window {win} to {tab}')
        _osascript(f'tell application "{_as_str(app)}" to set index of window {win} to 1')
        open_app(app)
        return True
    except RuntimeError:
        return False


def tab_url(win: int, tab: int, app: str | None = None) -> str | None:
    app = app or current_browser()
    if not app:
        return None
    try:
        return _osascript(f'tell application "{_as_str(app)}" to return URL of tab {tab} of window {win}') or None
    except RuntimeError:
        return None


def open_in_new_tab(url: str, app: str | None = None) -> bool:
    app = app or current_browser()
    if not app:
        open_url(url)
        return True
    try:
        _osascript(f'tell application "{_as_str(app)}" to make new tab at end of window 1 '
                   f'with properties {{URL:"{_as_str(url)}"}}')
        return True
    except RuntimeError:
        open_url(url)
        return True


def close_tab(win: int, tab: int, app: str | None = None) -> bool:
    app = app or current_browser()
    if not app:
        return False
    try:
        _osascript(f'tell application "{_as_str(app)}" to close tab {tab} of window {win}')
        return True
    except RuntimeError:
        return False


def accessibility_ok() -> bool:
    """True if this process may drive other apps. Uses the non-blocking API check first;
    AXIsProcessTrusted() returns immediately, unlike a System Events round-trip."""
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore

        return bool(AXIsProcessTrusted())
    except Exception:
        pass
    try:
        _osascript('tell application "System Events" to get name of first process', timeout=2.0)
        return True
    except Exception:
        return False
