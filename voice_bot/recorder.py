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


def _archive_previous(path: Path, started_at: float) -> None:
    """Move an existing transcript aside instead of overwriting it.

    Re-running a scenario used to destroy the previous transcript. That loses
    the record of earlier attempts, which is exactly the evidence you want when
    a bug reproduces across runs.
    """
    if not path.exists():
        return
    archive = TRANSCRIPTS_DIR / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(path.stat().st_mtime))
    path.replace(archive / f"{path.stem}-{stamp}{path.suffix}")


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
    _archive_previous(path, record.started_at)
    path.write_text("\n".join(lines), encoding="utf-8")

    json_path = TRANSCRIPTS_DIR / f"call-{s.slug}.json"
    _archive_previous(json_path, record.started_at)
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


def _correct_duration(scenario_slug: str, actual_seconds: float) -> None:
    """Rewrite a transcript's duration to match the recording.

    Our timer measures from stream start to hangup, which overstates the call
    if the machine suspends. The recording length is what actually happened.
    """
    json_path = TRANSCRIPTS_DIR / f"call-{scenario_slug}.json"
    if not json_path.exists():
        return
    data = json.loads(json_path.read_text(encoding="utf-8"))
    recorded = data.get("duration_seconds", 0)
    # Only correct a clear overstatement; small differences are just timing.
    if recorded <= actual_seconds * 1.5:
        return

    log.warning(
        "call-%s: logged duration %.0fs but the recording is %.0fs — correcting",
        scenario_slug, recorded, actual_seconds,
    )
    data["duration_seconds"] = round(actual_seconds, 1)
    data["duration_note"] = (
        f"Wall-clock timer read {recorded:.0f}s; corrected to the recording's "
        f"{actual_seconds:.0f}s (the host machine suspended mid-call)."
    )
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    txt_path = TRANSCRIPTS_DIR / f"transcript-{scenario_slug}.txt"
    if txt_path.exists():
        minutes, seconds = divmod(int(actual_seconds), 60)
        text = txt_path.read_text(encoding="utf-8")
        lines = []
        for line in text.splitlines():
            if line.startswith("Duration ...."):
                line = f"Duration ......... {minutes}:{seconds:02d}"
            lines.append(line)
        txt_path.write_text("\n".join(lines), encoding="utf-8")


async def download_recording(
    call_sid: str,
    scenario_slug: str,
    account_sid: str,
    auth_token: str,
    *,
    attempts: int = 20,
    delay: float = 5.0,
) -> Path | None:
    """Poll Twilio for the call's recording and save it as MP3.

    Two separate waits are needed. The recording *resource* appears in the list
    almost immediately, but its `status` stays "processing" while Twilio encodes
    the audio — fetching the .mp3 during that window returns 404. So we wait for
    status == "completed", then still retry the media fetch, because the
    resource can be marked complete a moment before the media is servable.
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
                recording = recordings[0]
                status = recording.get("status", "")
                if status == "completed":
                    recording_sid = recording["sid"]
                    # Twilio's duration is authoritative. Our own timer can
                    # overstate a call if the host suspends mid-conversation.
                    try:
                        _correct_duration(scenario_slug, float(recording["duration"]))
                    except (KeyError, TypeError, ValueError):
                        pass
                    log.info(
                        "Recording %s ready (%ss, %s channel(s))",
                        recording_sid, recording.get("duration"), recording.get("channels"),
                    )
                    break
                if status in ("failed", "absent"):
                    log.error("Twilio reports recording status %r for %s", status, call_sid)
                    return None
                log.info("Recording still %s (attempt %d/%d)...", status, attempt, attempts)
            else:
                log.info("Recording not listed yet (attempt %d/%d)...", attempt, attempts)
            await asyncio.sleep(delay)

        if not recording_sid:
            log.error(
                "No completed recording for call %s after %ds. It may still be "
                "processing — run `python -m voice_bot.fetch_recordings` to retry.",
                call_sid, int(attempts * delay),
            )
            return None

        media_url = f"{TWILIO_API}/Accounts/{account_sid}/Recordings/{recording_sid}.mp3"
        audio = None
        for attempt in range(1, 11):
            try:
                response = await client.get(media_url, auth=auth, follow_redirects=True)
                if response.status_code == 404:
                    log.info("Media not servable yet (attempt %d/10)...", attempt)
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                audio = response.content
                break
            except httpx.HTTPError as exc:
                log.warning("Recording download failed (attempt %d): %s", attempt, exc)
                await asyncio.sleep(delay)

        if audio is None:
            log.error(
                "Could not download recording %s. Retry later with "
                "`python -m voice_bot.fetch_recordings`.", recording_sid,
            )
            return None

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = RECORDINGS_DIR / f"call-{scenario_slug}.mp3"
    path.write_bytes(audio)
    log.info("Recording saved: %s (%.1f KB)", path.name, len(audio) / 1024)
    return path
