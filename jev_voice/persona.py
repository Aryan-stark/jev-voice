"""Persona phrasing for spoken replies. Pure code: no model, no latency."""
from __future__ import annotations

import os
import random
import re

from .tts import PERSONA

USER_NAME = os.environ.get("USER_NAME", "Wayne")
# How the butler addresses you. Weighted: "sir" most often, the grander ones as a treat.
HONORIFICS = ["sir"] * 5 + ["my lord"] * 3 + ["your lordship", "your grace", "my liege", f"master {USER_NAME}", "your excellency"]


def _h() -> str:
    return random.choice(HONORIFICS)

_ALFRED = {
    "On it.": ["Right away, {h}.", "At once, {h}.", "Very good, {h}."],
    "Done.": ["Done, {h}.", "Very good, {h}.", "As you wish.", "Quite so."],
    "Bye.": ["Very good, {h}. I shall be in the study.", "Good night, {h}."],
    "Ready.": ["At your service, {h}.", "Ready when you are, {h}."],
    "Not sure what you meant.": ["I'm afraid I didn't quite catch that, {h}.", "Beg your pardon, {h}?"],
    "That failed.": ["I'm afraid that didn't work, {h}.", "Regrettably, that failed, {h}."],
    "I don't see that app.": ["I don't believe that application is installed, {h}."],
    "Locking.": ["Securing the premises, {h}."],
    "Muted.": ["Silence, {h}."],
    "Screenshot saved to the desktop.": ["Captured and filed on the desktop, {h}."],
}
# Crisp, unhurried, faintly dry. Addresses you by name rather than by honorific.
_JARVIS = {
    "Done.": ["Done, {n}.", "Very good, {n}.", "Taken care of.", "Consider it handled.", "Already done."],
    "Bye.": ["Powering down, {n}.", "I'll be here if you need me, {n}.", "Standing by."],
    "Ready.": ["At your service, {n}.", "Online and ready, {n}.", "Systems ready, {n}.", "Good to see you, {n}."],
    "Not sure what you meant.": ["I didn't catch that, {n}.", "Could you run that by me again, {n}?", "I'm not following, {n}."],
    "That failed.": ["That didn't go through, {n}.", "I'm afraid that failed, {n}.", "No luck with that one, {n}."],
    "I don't see that app.": ["That application isn't installed, {n}.", "I can't find that one, {n}."],
    "Locking.": ["Securing the workstation, {n}.", "Locking up."],
    "Muted.": ["Muted.", "Silenced, {n}."],
    "Unmuted.": ["Sound restored, {n}."],
    "Louder.": ["Turning it up.", "Louder, {n}."],
    "Quieter.": ["Bringing it down.", "Quieter, {n}."],
    "Dictating.": ["Listening, {n}. Go ahead.", "Dictation active, {n}."],
    "Done dictating.": ["Dictation closed, {n}.", "Got all that, {n}."],
    "Screenshot saved to the desktop.": ["Captured and saved to the desktop, {n}."],
    "There's nothing to undo.": ["Nothing to reverse, {n}."],
    "On it.": ["On it, {n}.", "Right away, {n}.", "Doing that now, {n}.", "Consider it done, {n}."],
}
_COWBOY = {
    "On it.": ["On it, partner.", "Right away.", "Sure thing."],
    "Done.": ["Done and dusted.", "Yep.", "There ya go, partner.", "Easy as pie."],
    "Bye.": ["Happy trails.", "See ya 'round, partner."],
    "Ready.": ["Ready when you are, partner.", "Saddled up."],
    "Not sure what you meant.": ["Come again, partner?", "Didn't quite catch that."],
    "That failed.": ["Well, that horse done bucked.", "That one didn't take."],
    "I don't see that app.": ["Ain't got that one in the barn."],
    "Locking.": ["Lockin' up the ranch."],
    "Muted.": ["Hushed."],
    "Screenshot saved to the desktop.": ["Snapped it. It's on the desktop."],
}
_OPENING = {
    "jarvis": ["Opening {x}, {n}.", "{x}, coming up.", "Bringing up {x}, {n}.", "Right away, {n}."],
    "alfred": ["{x}, {h}.", "Bringing up {x}, {h}.", "Right away, {h}. {x}.", "As you command, {h}. {x}."],
    "cowboy": ["Rustlin' up {x}.", "{x}, comin' right up.", "Yep. {x}."],
}
_SEARCH = {
    "jarvis": ["Searching {e} for {q}, {n}.", "Looking up {q} on {e}.", "{q}, on {e}. One moment, {n}."],
    "alfred": ["Searching {e} for {q}, {h}.", "Right away, {h}. {q}, on {e}.", "{q}, on {e}. Consider it done, {h}."],
    "cowboy": ["Huntin' down {q} on {e}.", "Lookin' up {q}."],
}


_BANKS = {"alfred": _ALFRED, "cowboy": _COWBOY, "jarvis": _JARVIS}


def flavor(reply: str) -> str:
    bank = _BANKS.get(PERSONA)
    if bank is None or not reply:
        return reply
    if reply in bank:
        return random.choice(bank[reply]).replace("{h}", _h()).replace("{n}", USER_NAME)
    m = re.match(r"^Opening (.+)\.$", reply)
    if m:
        return random.choice(_OPENING[PERSONA]).format(x=m.group(1), h=_h(), n=USER_NAME)
    m = re.match(r"^Searching (.+?) for (.+)\.$", reply)
    if m:
        return random.choice(_SEARCH[PERSONA]).format(e=m.group(1), q=m.group(2), h=_h(), n=USER_NAME)
    if PERSONA == "alfred" and reply.endswith("."):
        return reply[:-1] + f", {_h()}."
    if PERSONA == "jarvis" and reply.endswith(".") and len(reply) < 60:
        return reply[:-1] + f", {USER_NAME}."
    return reply
