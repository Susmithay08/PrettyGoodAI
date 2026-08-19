#!/usr/bin/env python3
"""Pretty Good AI — automated patient voice bot.

    python main.py --scenario 1          # run one scenario
    python main.py --all                 # run all 16, 60s apart
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

import uvicorn
from twilio.rest import Client as TwilioClient

from voice_bot import server
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


def open_tunnel(config: Config) -> str:
    """Return the public https base URL Twilio should call back to."""
    if config.public_url:
        url = config.public_url.rstrip("/")
        log.info("Using PUBLIC_URL from environment: %s", url)
        return url

    from pyngrok import conf, ngrok

    if config.ngrok_auth_token:
        conf.get_default().auth_token = config.ngrok_auth_token
    tunnel = ngrok.connect(config.server_port, "http", bind_tls=True)
    url = tunnel.public_url.replace("http://", "https://")
    log.info("ngrok tunnel: %s -> :%d", url, config.server_port)
    return url


# --------------------------------------------------------------------------
# One call
# --------------------------------------------------------------------------

def place_call(config: Config, twilio: TwilioClient, base_url: str, run: server.CallRun) -> str:
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
    else:
        scenarios = all_scenarios()

    if args.voice:
        scenarios = [dataclasses.replace(s, voice=args.voice) for s in scenarios]

    start_server(config)
    base_url = open_tunnel(config)
    twilio = TwilioClient(config.twilio_account_sid, config.twilio_auth_token)

    log.info("Target line: %s", config.target_phone_number)
    log.info("Running %d scenario(s)", len(scenarios))

    succeeded = 0
    for index, scenario in enumerate(scenarios):
        try:
            if await run_scenario(config, twilio, base_url, scenario):
                succeeded += 1
        except KeyboardInterrupt:
            log.warning("Interrupted by user")
            break
        except Exception as exc:  # noqa: BLE001 — never let one call stop the batch
            log.exception("Scenario %s failed: %s", scenario.slug, exc)

        if index < len(scenarios) - 1:
            log.info("Waiting %ds before the next call...", config.gap_between_calls)
            await asyncio.sleep(config.gap_between_calls)

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
    group.add_argument("--all", action="store_true", help="run all 16 scenarios in sequence")
    group.add_argument("--list", action="store_true", help="list the scenarios and exit")
    parser.add_argument(
        "--voice", choices=["female", "male"], help="override the scenario's voice"
    )
    args = parser.parse_args()

    if args.list:
        print_scenarios()
        return 0
    if not args.scenario and not args.all:
        parser.print_help()
        return 1

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        log.warning("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
