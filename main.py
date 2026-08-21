#!/usr/bin/env python3
"""Pretty Good AI — automated patient voice bot.

    python main.py --scenario 1          # run one scenario
    python main.py --all                 # run all 16, 60s apart
    python main.py --scenarios 6,7,11-14  # run only specific ones
    python main.py --scenario 7 --voice female
    python main.py --list                # show the scenario table

The script owns the whole lifecycle: it opens an ngrok tunnel, boots the FastAPI
media server, places the outbound call, waits for the conversation to finish,
then saves the transcript and downloads the MP3.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
import threading
import time

import httpx
import uvicorn
from twilio.rest import Client as TwilioClient

from voice_bot import server, tunnel
from voice_bot.config import Config, ConfigError, load_config
from voice_bot.recorder import download_recording, write_transcript
from voice_bot.scenarios import Scenario, all_scenarios, get_scenario

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)-22s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
log = logging.getLogger("main")


class TTSUnavailable(RuntimeError):
    """Text-to-speech stopped working — every later call would be silent."""


# --------------------------------------------------------------------------
# Infrastructure
# --------------------------------------------------------------------------

def start_server(config: Config) -> threading.Thread:
    server.configure(config)
    uvicorn_config = uvicorn.Config(
        server.app, host="0.0.0.0", port=config.server_port, log_level="warning"
    )
    instance = uvicorn.Server(uvicorn_config)
    thread = threading.Thread(target=instance.run, daemon=True, name="media-server")
    thread.start()

    for _ in range(50):
        if instance.started:
            log.info("Media server listening on :%d", config.server_port)
            return thread
        time.sleep(0.1)
    raise RuntimeError("Media server failed to start")


def open_tunnel(config: Config) -> tuple[str, object]:
    """Return the public https base URL Twilio calls back to, plus a shutdown hook."""
    url, closer = tunnel.open_tunnel(
        config.server_port,
        public_url=config.public_url,
        ngrok_token=config.ngrok_auth_token,
    )
    server.set_public_url(url)
    return url, closer


# --------------------------------------------------------------------------
# One call
# --------------------------------------------------------------------------

def place_call(
    config: Config, twilio: TwilioClient, base_url: str, run: server.CallRun,
    *, attempts: int = 3,
) -> str:
    """Dial the test line, retrying transient network failures.

    A one-off TLS reset to api.twilio.com would otherwise abandon the whole
    scenario before the call was ever placed.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _create_call(config, twilio, base_url, run)
        except Exception as exc:  # noqa: BLE001 — twilio wraps many transports
            last = exc
            log.warning("Call attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(3 * attempt)
    raise last  # type: ignore[misc]


def _create_call(config: Config, twilio: TwilioClient, base_url: str, run: server.CallRun) -> str:
    call = twilio.calls.create(
        to=config.target_phone_number,
        from_=config.twilio_phone_number,
        url=f"{base_url}/twiml?run={run.run_id}",
        status_callback=f"{base_url}/status",
        status_callback_event=["initiated", "answered", "completed"],
        record=True,
        recording_channels="dual",
        time_limit=config.max_call_seconds,
    )
    log.info("Dialing %s ... Call SID %s", config.target_phone_number, call.sid)
    return call.sid


async def run_scenario(
    config: Config, twilio: TwilioClient, base_url: str, scenario: Scenario
) -> bool:
    banner = f" SCENARIO {scenario.slug} — {scenario.name} "
    log.info("=" * 72)
    log.info(banner.center(72, "="))
    log.info("Bug target: %s", scenario.bug_target)
    log.info("=" * 72)

    run = server.register_run(scenario)
    try:
        call_sid = place_call(config, twilio, base_url, run)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not place the call: %s", exc)
        return False

    if not run.record.call_sid:
        run.record.call_sid = call_sid

    # The conversation runs on the server's event loop; wait for it here.
    timeout = config.max_call_seconds + 90
    deadline = time.monotonic() + timeout
    while not run.finished.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.5)

    if not run.finished.is_set():
        log.error("Call did not complete within %ds — abandoning", timeout)
        run.record.note("Runner timed out waiting for the call to end")
        try:
            twilio.calls(call_sid).update(status="completed")
        except Exception:  # noqa: BLE001
            pass

    turns = len(run.record.turns)
    minutes, seconds = divmod(int(run.record.duration), 60)
    log.info("Call finished — %d turns, %d:%02d", turns, minutes, seconds)

    if run.tts_dead:
        raise TTSUnavailable("text-to-speech failed mid-call")

    write_transcript(run.record)
    await download_recording(
        run.record.call_sid or call_sid,
        scenario.slug,
        config.twilio_account_sid,
        config.twilio_auth_token,
    )
    return turns >= 4  # a one-question hang-up doesn't count as a real call


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_scenario_list(spec: str) -> list[int]:
    """Parse "6,7,11-14" into [6, 7, 11, 12, 13, 14]."""
    numbers: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            numbers.extend(range(int(start), int(end) + 1))
        else:
            numbers.append(int(part))
    seen: set[int] = set()
    return [n for n in numbers if not (n in seen or seen.add(n))]


# Roughly what one full conversation costs in ElevenLabs credits, measured
# from real calls: ~40 turns of ~12 words at ~0.5 credits per character.
CREDITS_PER_CALL = 1500


def _report_budget(config: Config, planned: int) -> None:
    """Warn up front if the account can't fund the whole run."""
    try:
        response = httpx.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": config.elevenlabs_api_key},
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError:
        return  # budget info is a nicety; never block a run over it

    remaining = max(0, data.get("character_limit", 0) - data.get("character_count", 0))
    affordable = remaining // CREDITS_PER_CALL
    log.info(
        "ElevenLabs credits: %s remaining (~%d call%s at ~%s each)",
        f"{remaining:,}", affordable, "" if affordable == 1 else "s", f"{CREDITS_PER_CALL:,}",
    )
    if planned > affordable:
        log.warning(
            "Planning %d calls but only ~%d are funded. The run will stop when "
            "credits run out — consider `--scenarios` to pick the ones you need.",
            planned, affordable,
        )


async def preflight(config: Config, planned: int = 1) -> bool:
    """Confirm text-to-speech works before spending money on calls.

    A dead TTS account doesn't crash — it produces a silent caller, and the
    far end hangs up after ~45s. Left unchecked that burns the whole batch.
    """
    from voice_bot.voice import Speaker

    speaker = Speaker(
        api_key=config.elevenlabs_api_key,
        voice_id=config.voice_id("female"),
        model=config.elevenlabs_model,
    )
    try:
        audio = await speaker.synthesize("Testing one two three.")
    finally:
        await speaker.aclose()

    if audio:
        log.info("Preflight OK — text-to-speech returned %.1fs of audio", len(audio) / 8000)
        _report_budget(config, planned)
        return True

    log.error("=" * 72)
    log.error("PREFLIGHT FAILED — text-to-speech produced no audio.")
    if speaker.fatal_error:
        log.error("  %s", speaker.fatal_error)
    log.error("  Calls would connect but the caller would be silent, and the")
    log.error("  far end would hang up. Not dialling.")
    log.error("  Check the account with: python -m voice_bot.voices")
    log.error("=" * 72)
    return False


def print_scenarios() -> None:
    print(f"\n{'#':<4}{'Name':<42}{'Voice':<9}Bug target")
    print("-" * 110)
    for s in all_scenarios():
        print(f"{s.number:<4}{s.name:<42}{s.voice:<9}{s.bug_target}")
    print()


async def amain(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.scenario:
        scenarios = [get_scenario(args.scenario)]
    elif args.scenarios:
        scenarios = [get_scenario(n) for n in parse_scenario_list(args.scenarios)]
    else:
        scenarios = all_scenarios()

    if args.voice:
        scenarios = [dataclasses.replace(s, voice=args.voice) for s in scenarios]

    if not await preflight(config, planned=len(scenarios)):
        return 2

    start_server(config)
    try:
        base_url, close_tunnel = open_tunnel(config)
    except RuntimeError as exc:
        log.error("Could not open a public tunnel:\n%s", exc)
        return 2

    twilio = TwilioClient(config.twilio_account_sid, config.twilio_auth_token)

    log.info("Target line: %s", config.target_phone_number)
    log.info("Running %d scenario(s)", len(scenarios))

    succeeded = 0
    try:
        for index, scenario in enumerate(scenarios):
            try:
                if await run_scenario(config, twilio, base_url, scenario):
                    succeeded += 1
            except KeyboardInterrupt:
                log.warning("Interrupted by user")
                break
            except TTSUnavailable:
                log.error("=" * 72)
                log.error("STOPPING THE BATCH — text-to-speech is no longer working.")
                log.error("  The caller would be silent on every remaining call.")
                log.error("  Scenarios %s onward were not attempted.", scenario.slug)
                log.error("=" * 72)
                break
            except Exception as exc:  # noqa: BLE001 — never let one call stop the batch
                log.exception("Scenario %s failed: %s", scenario.slug, exc)

            if index < len(scenarios) - 1:
                log.info("Waiting %ds before the next call...", config.gap_between_calls)
                await asyncio.sleep(config.gap_between_calls)
    finally:
        close_tunnel()

    log.info("=" * 72)
    log.info("Done — %d/%d calls produced a real conversation", succeeded, len(scenarios))
    log.info("Transcripts: ./transcripts   Recordings: ./recordings")
    return 0 if succeeded else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Automated patient bot that calls the Pretty Good AI test line.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--scenario", type=int, metavar="N", help="run a single scenario (1-16)")
    group.add_argument(
        "--scenarios", metavar="LIST",
        help="run specific scenarios, e.g. 6,7,11-14 (skips ones already done)",
    )
    group.add_argument("--all", action="store_true", help="run all 16 scenarios in sequence")
    group.add_argument("--list", action="store_true", help="list the scenarios and exit")
    parser.add_argument(
        "--voice", choices=["female", "male"], help="override the scenario's voice"
    )
    args = parser.parse_args()

    if args.list:
        print_scenarios()
        return 0
    if not args.scenario and not args.scenarios and not args.all:
        parser.print_help()
        return 1

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        log.warning("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
