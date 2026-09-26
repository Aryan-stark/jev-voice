# Jev Voice

Talk to your Mac. You speak, it opens apps, types, searches, scrolls, presses keys.

Everything runs locally except one ~440 ms call to **Jev** (TypeSafe's System One
model), which turns the transcript into a typed action plus typed arguments in a
single fan-out request. Jev never generates text; code produces candidate values
and Jev *selects*. Code owns execution.

```
speech
  │
  ├─ energy VAD ─► whisper.cpp (Metal, ~56 ms)         local, nothing leaves the Mac
  │
  ├─ wake word ("Sofia") ─► Jev  (1 request, ~440 ms)  classifier over 21 actions
  │                          │
  │        confident ────────┤                         → execute
  │                          │
  │   none-but-addressed, ───┘                         → escalate
  │   or low confidence
  │        │
  │        └─ local planner (Ollama, ~1 s, free)       plans from the same tools,
  │                          │                          or refuses outright
  │                          ▼
  └────────────────► risk gate ─► agent loop ─► verify
                      (HIGH asks    (retries
                       first)        timing failures)
```

Two properties matter more than the model:

- **A classifier is never forced to guess.** Jev must pick one of its actions, so an
  out-of-vocabulary request used to become the nearest neighbour and run — "mute slack
  notifications until 3pm" routed to `system(lock)` and locked the screen. Those cases
  now go to a local planner that is allowed to say *"I can't do that"*.
- **Nothing destructive happens unasked.** Every action carries a risk level; above
  MEDIUM it is held and spoken back for confirmation, answered locally by yes/no.

## Setup (macOS, Apple Silicon)

```sh
cp .env.example .env                       # add your TYPESAFE_API_KEY from console.typesafe.ai
./scripts/setup.sh

# The escalation tier — without this, out-of-vocabulary requests are refused
# outright instead of being planned.
brew install ollama && ollama serve &
ollama pull qwen3:4b-instruct-2507-q4_K_M
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

## Latency (MacBook Air M5 / 16 GB, measured)

| Stage | Time |
| --- | --- |
| End-of-speech detection | 550 ms of silence (tune `VADConfig.end_silence_ms`) |
| whisper.cpp base.en | ~56 ms |
| Jev fan-out | 380–630 ms (median ~440) |
| Local planner, when it escalates | ~1 s |
| Execute + verify | 100–900 ms, depending on the action |

Only the ~10–20 % of commands Jev is unsure about pay the planner cost; the rest take the
fast path. `jev-report` gives these numbers for your own machine rather than this table.

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
  main.py       loop, CLI, escalation, risk gate, dictation, compound handling
  brain.py      Jev questions, candidate extraction, Plan
  actions.py    macOS execution (apps, keystrokes, clipboard, Notes, tabs, Slack…)
  tools.py      capability registry — one definition per action, with risk metadata
  planner.py    escalation tier: local model plans from the registry, or refuses
  agent.py      act → observe → verify → repair, with failure classification
  context.py    what the machine looks like now, gathered lazily
  runtime.py    session state: dictation, recent turns, undo, held actions
  macros.py     saved multi-step routines (macros.json)
  telemetry.py  per-utterance event log + `jev-report`
  ax.py         Accessibility reads, used to verify before acting
  audio.py      mic + VAD endpointing
  stt.py        whisper-server client
  tts.py        macOS `say` / ElevenLabs
  config.py     env / thresholds
  hotkey.py     Caps Lock (remapped to F18) global key tap
  overlay.py    floating transcription pill (AppKit)
  persona.py    jarvis / butler / cowboy phrasing
macros.json     your routines
scripts/
  setup.sh      one-shot install: deps, model, Caps Lock remap, launcher, permissions
```

## The capability registry (`tools.py`)

Every action is declared once. The planner derives its tool schemas from it, the macro
runner dispatches through it, and the risk gate reads its metadata — so adding a
capability in one place makes it plannable, scriptable and gated at the same time.

Each tool carries `risk` (NONE…CRITICAL), `reversible`, and `needs_focus` — the last
because steps that need keyboard focus can never be run concurrently: `open -a` fights
over frontmost and keystrokes land wherever focus happens to be.

Risk can escalate per call: `system(empty_trash)` is HIGH while `system(toggle_dark_mode)`
is MEDIUM.

## The planner (`planner.py`)

Runs locally and offline through [Ollama](https://ollama.com). Default model:
`qwen3:4b-instruct-2507-q4_K_M`.

It **must be a non-thinking model.** Qwen3's hybrid models reason in the visible content
stream even with `think=false`, costing 7–25 s per plan; the `-instruct-2507` line has no
thinking mode and answers in ~1 s with better accuracy (9/9 vs timeouts on the same set).

```sh
ollama pull qwen3:4b-instruct-2507-q4_K_M
```

`PLANNER=0` disables it — Jev then guesses, as it used to.

## The agent loop (`agent.py`)

Retries **timing** failures (an app slow to front, a Slack switcher not yet ready), which
are the ones that actually recur. It does not retry structural failures — an app that
isn't installed will not become installed on a second attempt.

Replanning is built but **off by default**, on measurement rather than principle: against
the real structural failures this system produces it repaired 1 of 4, and that one turned
"open Notion" (not installed) into a web search for *"how to open Notion on Mac"*, which
it reported as success. A plausible-looking wrong action claiming `ok` is worse than a
clean failure. `AGENT_MAX_REPLANS=1` opts back in.

## Context (`context.py`)

"Send this to him" can't be answered from the sentence — it needs the state of the
machine. With Slack open on a conversation and text selected:

```
"send this to him"  →  slack(to="@Rahul", text="Meeting moved to 4 PM")
```

*"him"* comes from the active window title, *"this"* from the selection. With nothing
selected and no conversation open, the same phrase **refuses** rather than inventing a
recipient.

Gathered lazily, because it isn't free: the frontmost app costs ~1.5 ms and running apps
~2 ms, but enumerating browser tabs costs ~225 ms. Only utterances containing a pointing
word ("this", "that", "him", "it") pay for the expensive parts.

Reading the selection presses ⌘C — the system-wide Accessibility focused element is
unavailable and per-app reads come back empty on Electron apps, so the clean route
doesn't exist. The pasteboard is snapshotted and restored, and an *unchanged* pasteboard
is read as "nothing was selected". `SELECTION_VIA_CLIPBOARD=0` disables it.

## Telemetry (`jev-report`)

Every utterance writes one JSON line — transcript, per-stage timings, wake-word match,
confidence, escalation, refusal, outcome.

```sh
jev --ptt        # a session
jev-report       # median/p95 latency by stage, wake-word hit rate,
                 # escalation rate, refusals, failures with their errors
```

Blank transcripts are logged too: a false endpoint is a measurement, not a non-event.
Writing is best-effort — an unwritable log directory can never break a command.

## License

MIT
