"""Structured per-utterance event log.

Voice quality can only be argued about until it is measured, so every utterance writes
one JSON line: what was heard, how each stage did, and what happened. `jev-report`
turns a session into the numbers P0 asks for -- STT latency, wake-word hit rate,
escalation rate, refusals, end-to-end latency.

Writing is best-effort and never raises: telemetry must not be able to break a command.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator

LOG_DIR = Path(os.environ.get("JEV_LOG_DIR", Path.home() / ".cache" / "jev-voice" / "logs"))
ENABLED = os.environ.get("JEV_TELEMETRY", "1") not in ("0", "false", "no")

# Stages we time. Kept explicit so a report can assume the keys exist.
STAGES = ("audio", "stt", "jev", "planner", "execute", "verify")


@dataclass
class Event:
    """One utterance, start to finish."""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=time.time)
    mode: str = ""                     # ptt | smart | hold | always-on | text
    transcript: str = ""
    audio_seconds: float = 0.0
    addressed: bool | None = None      # did the wake word match?
    wake_source: str = ""              # wake_word | armed | caps_tap | text | none
    action: str = ""                   # Jev's chosen action
    confidence: float | None = None
    escalated: bool = False
    refused: bool = False
    plan_steps: list[str] = field(default_factory=list)
    ok: bool | None = None
    reply: str = ""
    error: str = ""
    timings_ms: dict[str, int] = field(default_factory=dict)

    def total_ms(self) -> int:
        return sum(self.timings_ms.get(s, 0) for s in STAGES)


class Recorder:
    """Collects one Event and writes it when the utterance finishes."""

    def __init__(self, mode: str) -> None:
        self.event = Event(mode=mode)
        self._t0: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.event.timings_ms[name] = int((time.perf_counter() - t0) * 1000)

    def mark(self, name: str, ms: int) -> None:
        """Record a stage whose duration was measured elsewhere."""
        self.event.timings_ms[name] = int(ms)

    def set(self, **kw: Any) -> None:
        for k, v in kw.items():
            if hasattr(self.event, k):
                setattr(self.event, k, v)

    def write(self) -> None:
        if not ENABLED:
            return
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            day = time.strftime("%Y-%m-%d", time.localtime(self.event.ts))
            row = asdict(self.event)
            row["total_ms"] = self.event.total_ms()
            with (LOG_DIR / f"{day}.jsonl").open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - telemetry must never break a command
            pass


# ------------------------------------------------------------------ reporting

def load(days: int = 1) -> list[dict[str, Any]]:
    """Most recent `days` log files, oldest first."""
    if not LOG_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for p in sorted(LOG_DIR.glob("*.jsonl"))[-days:]:
        for line in p.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _pct(values: list[float], q: float) -> float:
    """Linear-interpolated percentile, so a 2-sample median is the midpoint rather
    than the upper value."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def summarize(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (f"No telemetry yet. Logs go to {LOG_DIR}\n"
                "Run a session (jev --ptt) and try again.")
    n = len(rows)
    out: list[str] = [f"{n} utterance{'s' if n != 1 else ''}  ·  {LOG_DIR}", ""]

    # --- latency per stage ---
    out.append("latency (ms)        median      p95       max     n")
    for stage in STAGES + ("total",):
        key = (lambda r: r.get("total_ms", 0)) if stage == "total" \
            else (lambda r, s=stage: (r.get("timings_ms") or {}).get(s, 0))
        vals = [float(key(r)) for r in rows if key(r)]
        if not vals:
            continue
        out.append(f"  {stage:14} {_pct(vals, .5):8.0f} {_pct(vals, .95):8.0f} "
                   f"{max(vals):8.0f} {len(vals):5d}")

    # --- speech recognition quality ---
    heard = [r for r in rows if (r.get("transcript") or "").strip()]
    blank = n - len(heard)
    out += ["", "speech"]
    out.append(f"  transcribed          {len(heard)}/{n}"
               + (f"   ({blank} blank -- false endpoint or silence)" if blank else ""))
    if heard:
        words = [len((r["transcript"]).split()) for r in heard]
        secs = [r.get("audio_seconds", 0) for r in heard if r.get("audio_seconds")]
        out.append(f"  median words         {sorted(words)[len(words)//2]}")
        if secs:
            out.append(f"  median audio         {sorted(secs)[len(secs)//2]:.1f}s")

    # --- wake word ---
    addressed = [r for r in rows if r.get("addressed") is not None]
    if addressed:
        hit = sum(1 for r in addressed if r["addressed"])
        out += ["", "wake word"]
        out.append(f"  matched              {hit}/{len(addressed)}"
                   f"   ({100*hit/len(addressed):.0f}%)")
        misses = [r["transcript"] for r in addressed
                  if not r["addressed"] and (r.get("transcript") or "").strip()]
        for m in misses[:5]:
            out.append(f"    missed: {m[:60]!r}")

    # --- routing ---
    acted = [r for r in rows if r.get("action") and r["action"] != "none"]
    esc = [r for r in rows if r.get("escalated")]
    ref = [r for r in rows if r.get("refused")]
    failed = [r for r in rows if r.get("ok") is False]
    out += ["", "routing"]
    out.append(f"  actioned             {len(acted)}/{n}")
    out.append(f"  escalated to planner {len(esc)}   ({100*len(esc)/n:.0f}%)")
    out.append(f"  refused              {len(ref)}")
    out.append(f"  failed               {len(failed)}")
    confs = [r["confidence"] for r in rows if isinstance(r.get("confidence"), (int, float))]
    if confs:
        out.append(f"  median confidence    {_pct(confs, .5):.2f}"
                   f"   (p05 {_pct(confs, .05):.2f})")
    by_action: dict[str, int] = {}
    for r in acted:
        by_action[r["action"]] = by_action.get(r["action"], 0) + 1
    if by_action:
        top = sorted(by_action.items(), key=lambda kv: -kv[1])[:8]
        out += ["", "actions: " + ", ".join(f"{k}×{v}" for k, v in top)]

    if failed:
        out += ["", "failures"]
        for r in failed[:6]:
            out.append(f"  {r.get('transcript', '')[:44]!r} -> "
                       f"{(r.get('error') or r.get('reply') or '')[:50]}")
    return "\n".join(out)


def main() -> None:
    """Entry point for `jev-report`."""
    import argparse

    ap = argparse.ArgumentParser(prog="jev-report", description="Summarise voice sessions.")
    ap.add_argument("--days", type=int, default=1, help="how many log files to include")
    ap.add_argument("--raw", action="store_true", help="dump the raw JSON lines instead")
    a = ap.parse_args()
    rows = load(a.days)
    if a.raw:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return
    print(summarize(rows))
