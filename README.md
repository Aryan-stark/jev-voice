# Jev Voice

Talk to your Mac. You speak, it opens apps, types, searches, scrolls, presses keys.

Everything runs locally except one ~250 ms call to **Jev** (TypeSafe's System One
model), which turns the transcript into a typed action plus typed arguments in a
single fan-out request. Jev never generates text; code produces candidate values
and Jev *selects*. Code owns execution.

```
mic ─► energy VAD ─► whisper.cpp (Metal, ~100 ms) ─► Jev (1 request, ~250 ms) ─► macOS actions ─► `say`
```

## Setup (macOS, Apple Silicon)

```sh
cp .env.example .env                       # add your TYPESAFE_API_KEY from console.typesafe.ai
./scripts/setup.sh
```

The script installs whisper-cpp + ffmpeg, downloads the model, syncs the Python
env, remaps **Caps Lock → F18** with `hidutil` (persisted by a LaunchAgent so it
survives reboots), installs a `jev` launcher in `~/.local/bin`, and opens the
three permission panes. Grant the terminal app you launch from (Cursor / Terminal /
iTerm) **Microphone**, **Accessibility** and **Input Monitoring**. If a permission
is missing at launch, Jev Voice prompts for it and waits.

Undo the Caps Lock remap any time: `./scripts/uninstall-capslock.sh`.

## Run

```sh
jev                                 # hands-free: "Alfred, open chrome" (or tap CAPS LOCK, then speak)
jev --hold                          # hold CAPS LOCK to talk, release to run; no wake word
jev --always-on                     # open mic, EVERY utterance is a command (no wake word)
jev --ptt                           # push-to-talk in the terminal: Enter start / Enter stop
jev --device "RØDE"                 # pick a mic (uv run python -m sounddevice)
jev --text "open chrome and go to youtube" --dry-run   # test routing, no mic
```

**Hands-free mode (default):** the mic stays open and whisper transcribes every
utterance locally (~100 ms, nothing leaves the machine). Only utterances that name
the assistant (`WAKE_WORDS` in `.env`, default Alfred / Jarvis) go to Jev. After a
command you have `FOLLOWUP_SECONDS` (8) to chain more without the name: "Alfred,
open chrome" … "go to youtube" … "scroll down". Saying just "Alfred" chimes and
arms the next utterance. A Caps Lock tap does the same.

**Caps Lock modes (`--hold`):** hold it while speaking (Tink = recording, Pop = sent). A
short tap (<250 ms) latches hands-free recording; tap again to send. Caps Lock no
longer toggles capitals while the remap is installed.

## What you can say

| Say | Does |
| --- | --- |
| "open cursor", "switch to chrome" | `open -a` the matching installed app (Jev picks from the real app list) |
| "go to youtube", "go to stripe dot com" | opens the site |
| "search youtube for lofi hip hop", "google best ramen near me" | site-specific search |
| "type hello world and hit enter" | types into the focused field, optional submit |
| "close this tab", "select all and copy", "undo", "go back", "reload" | ~45 keyboard shortcuts |
| "scroll down a lot", "go to the top" | real scroll-wheel events |
| "volume up", "mute", "pause the music", "next song" | system volume / media keys |
| "take a screenshot", "open my downloads", "lock the screen", "toggle dark mode" | misc |
| "open notes and type buy milk and press enter" | compound: Jev flags it, code splits it, each step runs in order |
| "take a note buy milk", "note to self the wifi password is hunter2" | creates a note directly via AppleScript — no window, no keystrokes |
| "remind me to send the invoice", "add to my todo list pick up the parcel" | creates a Reminder instead (chosen in code from the phrasing) |
| "go to the claude tab", "close the youtube tab" | switches/closes an **already-open** browser tab, picked from the live tab list |
| "start dictating" … "stop dictating" | types everything you say verbatim; makes **no API call** while dictating |
| "undo that", "take that back" | reverses the last reversible action (volume, closed tab, dark mode) |
| "start my day", "focus mode", "end my day" | runs a saved multi-step routine from `macros.json` |

## Saved routines (`macros.json`)

Multi-step autonomy without a planner. Jev only chooses *which* routine you meant — a
Choice over the names in your file; the steps are ordinary code that verifies each one
before moving on, and stops at the first step it cannot confirm.

```json
{
  "start_my_day": {
    "description": "Open Slack, the browser and the editor, plus the daily tabs.",
    "steps": [
      {"do": "open_app", "app": "Slack"},
      {"do": "wait_for_app", "app": "Google Chrome"},
      {"do": "new_tab", "url": "https://mail.google.com"}
    ]
  }
}
```

Step kinds: `open_app`, `focus_app`, `wait_for_app`, `open_url`, `new_tab`, `shortcut`,
`type`, `note`, `reminder`, `volume`, `system`, `wait`. Loaded from `$JEV_MACROS`, else
`~/.config/jev-voice/macros.json`, else `./macros.json`. A malformed file is reported and
ignored rather than breaking voice control.

## Dictation mode

Say "start dictating" and every utterance is typed verbatim until "stop dictating". While
dictating, Jev is bypassed entirely — so it costs nothing, adds no latency, and words like
"open chrome" get *typed* rather than executed. Exits on the stop phrase, a Caps Lock tap,
or `DICTATION_IDLE_SECONDS` of silence. Refused in `--always-on`, where an open mic would
type every overheard remark into the focused window.

## Text entry

Text goes in via the clipboard (⌘V) when that is more reliable — anything long, multi-line,
emoji-bearing or quote-bearing — and via `keystroke` otherwise. The previous clipboard is
restored afterwards, but only if nothing else wrote to it in the meantime, so a ⌘C during
the paste window is never clobbered.

macOS offers no way to confirm a paste landed in the target field; only the clipboard
*write* is verified. If the field was not focused, the text is silently lost.

## What it can and cannot know

Perception is tiered by permission, which sets a hard ceiling on autonomy:

| Signal | Permission | Used |
| --- | --- | --- |
| Installed apps, **running** apps, frontmost app | none | yes |
| Browser tabs (titles + URLs), Notes, Reminders | Automation, per app | yes |
| Focused field contents, verifying typed text landed | Accessibility | not yet |
| Window titles | Screen Recording | no |

Jev also sees the last three turns (what you said, what ran, whether it worked), which is
what lets "undo that" and "close it" resolve at all — every API call is otherwise stateless.

## How the Jev layer works (`jev_voice/brain.py`)

One request per utterance with ~15 speculative questions evaluated in parallel:

- `action` — Choice over 13 action kinds.
- `app` — Choice over your installed apps (+ `none`); `site`, `engine`, `folder`,
  `shortcut`, `scroll_dir`, `volume_op`, `media_op`, `system_op` — Choices over
  closed sets whose keys are exactly what the executor accepts.
- `text` — Choice over **candidate spans** cut from the transcript by regex
  ("type X", "search for X", quoted text, whole utterance). Jev picks the one that
  is exactly the payload. This is the "select instead of generate" pattern.
- `submit`, `compound` — Nouls.

Code reads only the answers the chosen action needs. Plan confidence is the
minimum over the judgements used. Below `ACTION_MIN_CONFIDENCE` (0.35) it says
"not sure" instead of acting. Thresholds live in `jev_voice/config.py`.

## Latency (Mac mini M4, measured)

| Stage | Time |
| --- | --- |
| End-of-speech detection | 550 ms of silence (tune `VADConfig.end_silence_ms`) |
| whisper.cpp base.en | 80–130 ms |
| Jev fan-out | 170–420 ms |
| Execute + `say` | ~50–100 ms |

## Floating transcription pill

A small always-on-top bar at the top-center of the screen shows what whisper
heard, what Jev decided, and the result (gray idle · red listening · yellow
heard · blue thinking · green done · orange error). It never takes keyboard
focus. `OVERLAY=0` or `--no-overlay` hides it.

## Feedback

`FEEDBACK=ding` (default) plays a chime when an action completes and a low buzz
on failure. `FEEDBACK=voice` gives spoken replies from a posh butler persona
(`PERSONA=jarvis`, `alfred`, or `cowboy`) using the best British voice installed, or
ElevenLabs if `ELEVENLABS_API_KEY` is set (phrases cached to disk, so repeats are
instant).

## Layout

```
jev_voice/
  main.py     loop, CLI, compound handling
  brain.py    Jev questions, candidate extraction, Plan
  actions.py  macOS execution (open, keystrokes, scroll, volume, media keys…)
  audio.py    mic + VAD endpointing
  stt.py      whisper-server client
  tts.py      macOS `say`
  config.py   env / thresholds
  hotkey.py   Caps Lock (remapped to F18) global key tap
  overlay.py  floating transcription pill (AppKit)
  persona.py  butler / cowboy phrasing
scripts/
  setup.sh    one-shot install: deps, model, Caps Lock remap, launcher, permissions
```

## License

MIT
