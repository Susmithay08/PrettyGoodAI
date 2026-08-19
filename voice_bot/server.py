"""FastAPI webhook + Twilio Media Streams bridge.

Twilio connects a bidirectional WebSocket to /media. From there:

    Twilio audio (mulaw 8k) ──► Deepgram ──► text
                                              │
                                     Claude (PatientBrain)
                                              │
    Twilio audio (mulaw 8k) ◄── ElevenLabs ◄──┘

Turn-taking is driven by Deepgram's endpointing plus a silence timer, so the
bot waits for the agent to finish rather than talking over it — except in
scenario 9, where interrupting is the whole point.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

from html import escape

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from .audio import FRAME_BYTES, chunk_frames, silence
from .bot import PatientBrain
from .config import Config
from .recorder import CallRecord
from .scenarios import Scenario, get_scenario
from .transcriber import Transcriber
from .voice import Speaker

log = logging.getLogger(__name__)

PATIENT = "PATIENT (our bot)"
AGENT = "AGENT (Pretty Good AI)"

FRAME_SECONDS = 0.02
PREBUFFER_FRAMES = 20        # 400 ms of jitter buffer before we start pacing
ENDPOINT_DELAY = 0.65        # silence after agent speech before we reply
AGENT_TURN_TIMEOUT = 14.0    # give up waiting and say something anyway
GREETING_TIMEOUT = 8.0       # how long we let the agent greet us first


# --------------------------------------------------------------------------
# Run registry — main.py registers a run, then points Twilio at it by id.
# --------------------------------------------------------------------------

@dataclass
class CallRun:
    run_id: str
    scenario: Scenario
    record: CallRecord
    finished: threading.Event = field(default_factory=threading.Event)
    claimed: bool = False  # a media stream has attached to this run


_RUNS: dict[str, CallRun] = {}
_CONFIG: Config | None = None
_PUBLIC_BASE: str = ""


def configure(config: Config) -> None:
    global _CONFIG
    _CONFIG = config


def set_public_url(url: str) -> None:
    """Tell the server its own public hostname.

    The stream URL is built from this rather than from the inbound request's
    Host header — behind a tunnel or proxy the header is not something we
    want to depend on.
    """
    global _PUBLIC_BASE
    _PUBLIC_BASE = url.rstrip("/")


def register_run(scenario: Scenario) -> CallRun:
    run = CallRun(run_id=uuid.uuid4().hex[:12], scenario=scenario, record=CallRecord(scenario))
    _RUNS[run.run_id] = run
    return run


def get_run(run_id: str) -> CallRun | None:
    return _RUNS.get(run_id)


def _resolve_run(run_id: str, source: str) -> CallRun | None:
    """Find the run this media stream belongs to.

    Twilio does not reliably preserve the query string on a <Stream> URL, so
    the run id is passed three ways and we accept whichever survives. Calls are
    placed one at a time, so falling back to "the one unclaimed run" is safe
    and keeps a dropped id from killing the call.
    """
    run = _RUNS.get(run_id) if run_id else None
    if run is not None:
        log.info("Media stream matched run %s via %s", run_id, source)
        return run

    unclaimed = [r for r in _RUNS.values() if not r.claimed and not r.finished.is_set()]
    if len(unclaimed) == 1:
        run = unclaimed[0]
        log.warning(
            "Run id missing from the media stream (got %r) — falling back to the only "
            "pending run, %s", run_id, run.run_id,
        )
        return run

    log.error(
        "Cannot match media stream to a run (id=%r, %d unclaimed runs)",
        run_id, len(unclaimed),
    )
    return None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

app = FastAPI(title="Pretty Good AI — patient voice bot")


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "active_runs": len(_RUNS)}


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request) -> PlainTextResponse:
    """TwiML that hands the call's audio to our WebSocket."""
    run_id = request.query_params.get("run", "")
    if not run_id and request.method == "POST":
        form = await request.form()
        run_id = str(form.get("run", ""))

    if _PUBLIC_BASE:
        base = _PUBLIC_BASE.replace("https://", "wss://").replace("http://", "ws://")
    else:  # local testing without a tunnel
        port = f":{request.url.port}" if request.url.port not in (None, 443, 80) else ""
        base = f"wss://{request.url.hostname}{port}"

    # The run id rides in the URL *and* as a <Parameter>. Twilio delivers
    # <Parameter> values in the `start` frame's customParameters, which is the
    # documented channel and survives when the query string does not.
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<Stream url="{escape(f"{base}/media?run={run_id}", quote=True)}">'
        f'<Parameter name="run" value="{escape(run_id, quote=True)}" />'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )
    log.info("Served TwiML for run %r -> %s/media", run_id, base)
    return PlainTextResponse(body, media_type="application/xml")


@app.api_route("/status", methods=["GET", "POST"])
async def status_callback(request: Request) -> PlainTextResponse:
    form = await request.form() if request.method == "POST" else request.query_params
    log.info("Twilio status: %s -> %s", form.get("CallSid"), form.get("CallStatus"))
    return PlainTextResponse("", status_code=204)


async def _read_start_frame(websocket: WebSocket) -> dict | None:
    """Consume Twilio's `connected` / `start` frames; return the start message."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            message = json.loads(await websocket.receive_text())
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            return None
        if message.get("event") == "start":
            return message
    return None


@app.websocket("/media")
async def media(websocket: WebSocket) -> None:
    await websocket.accept()

    # Read `start` first: it carries customParameters, which is where the run
    # id reliably lives. Only then can we tell which scenario this call is.
    start = await _read_start_frame(websocket)
    if start is None or _CONFIG is None:
        log.error("Media stream never sent a usable `start` frame")
        await websocket.close()
        return

    run_id = websocket.query_params.get("run", "")
    source = "query string"
    if not run_id:
        run_id = start.get("start", {}).get("customParameters", {}).get("run", "")
        source = "customParameters"

    run = _resolve_run(run_id, source)
    if run is None:
        await websocket.close()
        return
    run.claimed = True

    session = MediaSession(websocket, _CONFIG, run)
    session.adopt_start(start)
    try:
        await session.run()
    except WebSocketDisconnect:
        log.info("Twilio disconnected the media stream")
    except Exception as exc:  # noqa: BLE001 — one bad call must not kill the server
        log.exception("Media session crashed: %s", exc)
        run.record.note(f"Media session error: {exc}")
    finally:
        await session.cleanup()
        run.record.ended_at = time.time()
        run.finished.set()


# --------------------------------------------------------------------------
# The call itself
# --------------------------------------------------------------------------

class MediaSession:
    def __init__(self, websocket: WebSocket, config: Config, run: CallRun) -> None:
        self.ws = websocket
        self.cfg = config
        self.scenario = run.scenario
        self.record = run.record
        self.stream_sid: str | None = None
        self.done = False

        gain = 0.35 if self.scenario.quiet else 1.0
        speed = 0.88 if self.scenario.quiet else 1.0
        self.speaker = Speaker(
            api_key=config.elevenlabs_api_key,
            voice_id=config.voice_id(self.scenario.voice),
            model=config.elevenlabs_model,
            gain=gain,
            speed=speed,
        )
        self.brain = PatientBrain(config.anthropic_api_key, config.claude_model, self.scenario)
        self.transcriber = Transcriber(
            config.deepgram_api_key,
            model=config.deepgram_model,
            language=self.scenario.language,
            on_partial=self._on_partial,
            on_final=self._on_final,
            on_utterance_end=self._on_utterance_end,
        )

        # Turn-taking state
        self._agent_buffer: list[str] = []
        self._last_agent_audio = time.monotonic()
        self._utterance_ended = False
        self._barge_in = asyncio.Event()
        self._speaking = False
        self._interrupt_playback = False
        self._marks: set[str] = set()
        self._started_at = time.monotonic()

    # -- main loop ---------------------------------------------------------

    def adopt_start(self, message: dict) -> None:
        """Apply the `start` frame the websocket handler already consumed."""
        start = message.get("start", {})
        self.stream_sid = start.get("streamSid")
        self.record.call_sid = start.get("callSid", "")
        self.record.started_at = time.time()
        self._started_at = time.monotonic()
        log.info(
            "Call %s connected — scenario %s (%s)",
            self.record.call_sid, self.scenario.slug, self.scenario.name,
        )

    async def run(self) -> None:
        if self.stream_sid is None:
            await self._await_stream_start()
        if self.done:
            return

        await self.transcriber.connect()
        pump = asyncio.create_task(self._pump_twilio())
        conversation = asyncio.create_task(self._converse())
        watchdog = asyncio.create_task(self._watchdog())

        done, pending = await asyncio.wait(
            {pump, conversation, watchdog}, return_when=asyncio.FIRST_COMPLETED
        )
        self.done = True
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error and not isinstance(error, WebSocketDisconnect):
                raise error

    async def _await_stream_start(self) -> None:
        """Consume Twilio's `connected` / `start` frames before doing anything."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                message = json.loads(await self.ws.receive_text())
            except (WebSocketDisconnect, RuntimeError):
                self.done = True
                return
            if message.get("event") == "start":
                self.stream_sid = message["start"]["streamSid"]
                self.record.call_sid = message["start"].get("callSid", "")
                self.record.started_at = time.time()
                self._started_at = time.monotonic()
                log.info(
                    "Call %s connected — scenario %s (%s)",
                    self.record.call_sid, self.scenario.slug, self.scenario.name,
                )
                return
        log.error("Never received a `start` event from Twilio")
        self.done = True

    async def _pump_twilio(self) -> None:
        """Forward inbound call audio to Deepgram; handle Twilio control frames."""
        while not self.done:
            try:
                message = json.loads(await self.ws.receive_text())
            except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
                break

            event = message.get("event")
            if event == "media":
                payload = message["media"]["payload"]
                await self.transcriber.send_audio(base64.b64decode(payload))
            elif event == "mark":
                self._marks.add(message["mark"]["name"])
            elif event == "stop":
                log.info("Twilio sent `stop` — the far end hung up")
                self.record.note("Far end ended the call")
                break
        self.done = True

    async def _watchdog(self) -> None:
        """Hard stop so a stuck call can never run forever."""
        while not self.done:
            await asyncio.sleep(1.0)
            if time.monotonic() - self._started_at > self.cfg.max_call_seconds:
                log.warning("Max call duration reached — hanging up")
                self.record.note("Hung up on max-duration watchdog")
                self.done = True

    async def _converse(self) -> None:
        # Let the agent deliver its greeting first — that's what a real caller does.
        heard_greeting = await self._wait_for_agent(GREETING_TIMEOUT)
        if self.done:
            return
        if heard_greeting:
            self.brain.record_agent(self._drain_agent())
        else:
            self.record.note("Agent did not greet within 8s — opened anyway")

        await self._say(self.scenario.opening_line)

        if self.scenario.pause_after_opening_ms:
            await self._send_silence(self.scenario.pause_after_opening_ms)

        while not self.done and not self.brain.should_hang_up:
            heard = await self._wait_for_agent(AGENT_TURN_TIMEOUT)
            if self.done:
                break

            nudge = None
            if heard:
                self.brain.record_agent(self._drain_agent())
            else:
                self.record.note("Agent went silent — prompting to keep the call alive")
                nudge = (
                    "The agent has gone quiet for a while. Say something natural to keep "
                    "the call moving — repeat your question or ask if they're still there."
                )

            line = await self.brain.next_line(nudge)
            if line:
                await self._say(line)
            elif not heard:
                # No agent audio and Claude had nothing to add: the call is over.
                break

        if not self.done:
            self.record.note("Bot ended the call (agenda complete)")
        self.done = True
        await self._hang_up()

    # -- Deepgram callbacks ------------------------------------------------

    async def _on_partial(self, text: str) -> None:
        self._last_agent_audio = time.monotonic()
        self._utterance_ended = False
        trigger = self.scenario.interrupt_after_words
        if trigger and not self._speaking and len(text.split()) >= trigger:
            self._barge_in.set()

    async def _on_final(self, text: str) -> None:
        self._last_agent_audio = time.monotonic()
        self._agent_buffer.append(text)
        log.info("AGENT: %s", text)

    async def _on_utterance_end(self) -> None:
        self._utterance_ended = True

    def _drain_agent(self) -> str:
        text = " ".join(self._agent_buffer).strip()
        self._agent_buffer.clear()
        self._utterance_ended = False
        if text:
            self.record.add(AGENT, text)
        return text

    async def _wait_for_agent(self, timeout: float) -> bool:
        """Block until the agent finishes a turn (or we decide to cut in)."""
        deadline = time.monotonic() + timeout
        self._barge_in.clear()
        while not self.done:
            await asyncio.sleep(0.08)
            if self._barge_in.is_set():
                self._barge_in.clear()
                self.record.note("Bot interrupted the agent mid-sentence (barge-in test)")
                return bool(self._agent_buffer)
            if self._agent_buffer:
                quiet_for = time.monotonic() - self._last_agent_audio
                if self._utterance_ended or quiet_for >= ENDPOINT_DELAY:
                    return True
            if time.monotonic() >= deadline:
                return bool(self._agent_buffer)
        return False

    # -- speaking ----------------------------------------------------------

    async def _say(self, text: str) -> None:
        if self.done or not text.strip():
            return

        audio = await self.speaker.synthesize(text)
        if not audio:
            self.record.note(f"TTS failed, line not spoken: {text[:60]!r}")
            return

        if self.scenario.quiet:
            # A shaky connection: dead air before the caller actually speaks.
            audio = silence(900) + audio

        log.info("PATIENT: %s", text)
        self.record.add(PATIENT, text)
        self.brain.record_self(text)

        self._speaking = True
        self._interrupt_playback = False
        try:
            await self._stream_audio(audio)
        finally:
            self._speaking = False

    async def _send_silence(self, milliseconds: int) -> None:
        self.record.note(f"Held {milliseconds}ms of silence")
        await self._stream_audio(silence(milliseconds))

    async def _stream_audio(self, ulaw: bytes) -> None:
        """Send μ-law to Twilio paced at real time so playback stays interruptible."""
        frames = chunk_frames(ulaw)
        next_send = time.monotonic()

        for index, frame in enumerate(frames):
            if self.done or self._interrupt_playback:
                break
            try:
                await self.ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(frame).decode("ascii")},
                }))
            except (WebSocketDisconnect, RuntimeError):
                self.done = True
                return

            # Burst the first few frames to build a jitter buffer, then pace.
            if index >= PREBUFFER_FRAMES:
                next_send += FRAME_SECONDS
                delay = next_send - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            else:
                next_send = time.monotonic()

        await self._await_playback(len(frames))

    async def _await_playback(self, frame_count: int) -> None:
        """Wait for Twilio to actually play out the buffered tail."""
        mark = uuid.uuid4().hex[:8]
        try:
            await self.ws.send_text(json.dumps({
                "event": "mark",
                "streamSid": self.stream_sid,
                "mark": {"name": mark},
            }))
        except (WebSocketDisconnect, RuntimeError):
            self.done = True
            return

        buffered = min(frame_count, PREBUFFER_FRAMES) * FRAME_SECONDS
        deadline = time.monotonic() + buffered + 3.0
        while not self.done and time.monotonic() < deadline:
            if mark in self._marks:
                self._marks.discard(mark)
                return
            await asyncio.sleep(0.05)

    async def _hang_up(self) -> None:
        """Closing the stream ends the <Connect> verb, which ends the call."""
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    # -- teardown ----------------------------------------------------------

    async def cleanup(self) -> None:
        self.done = True
        await self.transcriber.close()
        await self.speaker.aclose()
        await self.brain.aclose()
        self._drain_agent()
