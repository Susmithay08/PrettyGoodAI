"""Persisting call artifacts: transcripts (.txt/.json) and audio (.mp3).

Audio comes from Twilio's own dual-channel call recording rather than anything
we capture locally — it's the same audio a human reviewer would hear, and it
sidesteps clock drift between our TTS output and the inbound stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx

from .config import RECORDINGS_DIR, TRANSCRIPTS_DIR
from .scenarios import Scenario

log = logging.getLogger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01"


@dataclass
class Turn:
    speaker: str  # "PATIENT (our bot)" | "AGENT (Pretty Good AI)"
    text: str
    offset: float  # seconds since call start

    @property
    def timestamp(self) -> str:
        minutes, seconds = divmod(int(self.offset), 60)
        return f"{minutes}:{seconds:02d}"


@dataclass
class CallRecord:
    scenario: Scenario
    call_sid: str = ""
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    turns: list[Turn] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, speaker: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.turns.append(Turn(speaker, text, time.time() - self.started_at))

    def note(self, message: str) -> None:
        offset = time.time() - self.started_at
        self.notes.append(f"[{int(offset // 60)}:{int(offset % 60):02d}] {message}")

    @property
    def duration(self) -> float:
        return (self.ended_at or time.time()) - self.started_at


def write_transcript(record: CallRecord) -> Path:
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    s = record.scenario
    minutes, seconds = divmod(int(record.duration), 60)

    lines = [
        "=" * 72,
        f"CALL {s.slug} — {s.name}",
        "=" * 72,
        f"Scenario ......... {s.number} of 16",
        f"Voice ............ {s.voice}",
        f"Bug target ....... {s.bug_target}",
        f"Twilio Call SID .. {record.call_sid or 'n/a'}",
        f"Started .......... {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.started_at))}",
        f"Duration ......... {minutes}:{seconds:02d}",
        f"Audio ............ recordings/call-{s.slug}.mp3",
        "",
        "GOAL",
        f"  {s.goal}",
        "",
        "WHAT WE WERE WATCHING FOR",
        *[f"  - {item}" for item in s.watch_for],
        "",
        "=" * 72,
        "TRANSCRIPT",
        "=" * 72,
        "",
    ]

    for turn in record.turns:
        lines.append(f"[{turn.timestamp}] {turn.speaker}:")
        lines.append(f"    {turn.text}")
        lines.append("")

    if record.notes:
        lines += ["=" * 72, "PIPELINE NOTES", "=" * 72, *record.notes, ""]

    path = TRANSCRIPTS_DIR / f"transcript-{s.slug}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")

    json_path = TRANSCRIPTS_DIR / f"call-{s.slug}.json"
    json_path.write_text(json.dumps({
        "scenario": s.number,
        "name": s.name,
        "voice": s.voice,
        "bug_target": s.bug_target,
        "watch_for": s.watch_for,
        "call_sid": record.call_sid,
        "started_at": record.started_at,
        "duration_seconds": round(record.duration, 1),
        "turns": [asdict(t) for t in record.turns],
        "notes": record.notes,
    }, indent=2), encoding="utf-8")

    log.info("Transcript written: %s", path.name)
    return path


async def download_recording(
    call_sid: str,
    scenario_slug: str,
    account_sid: str,
    auth_token: str,
    *,
    attempts: int = 12,
    delay: float = 5.0,
) -> Path | None:
    """Poll Twilio for the call's recording and save it as MP3.

    Twilio finalizes recordings a few seconds after the call ends, so this
    retries rather than failing on the first empty response.
    """
    auth = (account_sid, auth_token)
    list_url = f"{TWILIO_API}/Accounts/{account_sid}/Calls/{call_sid}/Recordings.json"

    async with httpx.AsyncClient(timeout=60.0) as client:
        recording_sid = None
        for attempt in range(1, attempts + 1):
            try:
                response = await client.get(list_url, auth=auth)
                response.raise_for_status()
                recordings = response.json().get("recordings", [])
            except httpx.HTTPError as exc:
                log.warning("Recording lookup failed (attempt %d): %s", attempt, exc)
                recordings = []

            if recordings:
                recording_sid = recordings[0]["sid"]
                break
            log.info("Recording not ready yet (attempt %d/%d)...", attempt, attempts)
            await asyncio.sleep(delay)

        if not recording_sid:
            log.error("No recording found for call %s", call_sid)
            return None

        media_url = f"{TWILIO_API}/Accounts/{account_sid}/Recordings/{recording_sid}.mp3"
        try:
            audio = await client.get(media_url, auth=auth, follow_redirects=True)
            audio.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("Recording download failed: %s", exc)
            return None

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = RECORDINGS_DIR / f"call-{scenario_slug}.mp3"
    path.write_bytes(audio.content)
    log.info("Recording saved: %s (%.1f KB)", path.name, len(audio.content) / 1024)
    return path
