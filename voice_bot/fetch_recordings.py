"""Download any call recordings that are missing from ./recordings.

    python -m voice_bot.fetch_recordings

Twilio encodes a recording for a little while after the call ends, so a
download attempted immediately can 404 even though the audio is fine. This
walks the call metadata written alongside each transcript and fetches anything
that isn't on disk yet. Safe to re-run; it skips files it already has.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from .config import RECORDINGS_DIR, TRANSCRIPTS_DIR, load_config
from .recorder import download_recording

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("fetch")


async def main() -> int:
    config = load_config()
    metadata = sorted(TRANSCRIPTS_DIR.glob("call-*.json"))
    if not metadata:
        log.error("No call metadata in %s — run a call first.", TRANSCRIPTS_DIR)
        return 1

    force = "--force" in sys.argv

    missing: list[tuple[str, str]] = []
    for path in metadata:
        data = json.loads(path.read_text(encoding="utf-8"))
        slug = f"{data['scenario']:02d}"
        call_sid = data.get("call_sid", "")
        if not call_sid:
            log.warning("call-%s.json has no call_sid — skipping", slug)
            continue

        existing = RECORDINGS_DIR / f"call-{slug}.mp3"
        if existing.exists() and not force:
            size = existing.stat().st_size
            expected = data.get("duration_seconds", 0)
            # ~1 KB/s at Twilio's mp3 bitrate; anything far under that is a
            # stale or truncated file from an earlier run, not real audio.
            if expected and size < expected * 400:
                log.warning(
                    "call-%s.mp3 is only %.1f KB for a %.0fs call — looks stale, refetching",
                    slug, size / 1024, expected,
                )
            else:
                log.info("call-%s.mp3 already present (%.1f KB)", slug, size / 1024)
                continue
        missing.append((slug, call_sid))

    if not missing:
        log.info("Every call already has audio. Nothing to do.")
        return 0

    log.info("Fetching %d missing recording(s)...", len(missing))
    failed = 0
    for slug, call_sid in missing:
        path = await download_recording(
            call_sid, slug, config.twilio_account_sid, config.twilio_auth_token,
            attempts=6, delay=4.0,
        )
        if path is None:
            failed += 1
            log.error("call-%s: still unavailable (call %s)", slug, call_sid)

    log.info("Done — %d/%d recovered", len(missing) - failed, len(missing))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
