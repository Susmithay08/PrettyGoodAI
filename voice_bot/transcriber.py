"""Deepgram streaming speech-to-text.

We talk to Deepgram's WebSocket API directly rather than through their SDK: the
payload is small, and it keeps us off a fast-moving SDK's breaking changes for a
one-week project.

Audio in is the raw μ-law we receive from Twilio, so there is no transcoding on
the receive path either. Callbacks fire on the event loop:

  on_partial(text)          — interim words, used to detect that the agent
                              started talking (barge-in / turn-taking)
  on_final(text)            — a finalized transcript segment
  on_utterance_end()        — Deepgram thinks the speaker stopped
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable
from urllib.parse import urlencode

import websockets

log = logging.getLogger(__name__)

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
MAX_RECONNECTS = 5  # give up rather than thrash if the stream keeps dying

Callback = Callable[..., Awaitable[None]]


class Transcriber:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "nova-2-phonecall",
        language: str = "en-US",
        on_partial: Callback | None = None,
        on_final: Callback | None = None,
        on_utterance_end: Callback | None = None,
    ) -> None:
        self._api_key = api_key
        # `multi` (code-switching) is only available on nova-3.
        self._model = "nova-3" if language == "multi" else model
        self._language = language
        self._on_partial = on_partial
        self._on_final = on_final
        self._on_utterance_end = on_utterance_end

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._reader: asyncio.Task | None = None
        self._closed = False
        self._reconnects = 0

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        params = {
            "model": self._model,
            "language": self._language,
            "encoding": "mulaw",
            "sample_rate": "8000",
            "channels": "1",
            "punctuate": "true",
            "smart_format": "true",
            "interim_results": "true",
            "endpointing": "300",
            "utterance_end_ms": "1000",
            "vad_events": "true",
            "filler_words": "true",
        }
        url = f"{DEEPGRAM_URL}?{urlencode(params)}"
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {self._api_key}"},
            ping_interval=5,
            ping_timeout=20,
        )
        self._reader = asyncio.create_task(self._read_loop())
        log.info("Deepgram connected (model=%s, language=%s)", self._model, self._language)

    async def send_audio(self, ulaw: bytes) -> None:
        if self._closed:
            return
        if self._ws is None:
            await self._reconnect()
            return
        try:
            await self._ws.send(ulaw)
        except websockets.ConnectionClosed:
            # Deepgram drops the socket occasionally (keepalive timeout). If we
            # don't come back the bot is deaf for the rest of the call, which
            # looks like the far end going silent. Reconnect and keep going.
            log.warning("Deepgram socket dropped — reconnecting")
            self._ws = None
            await self._reconnect()

    async def _reconnect(self) -> None:
        """Re-establish the stream after a drop, without killing the call."""
        if self._closed or self._reconnects >= MAX_RECONNECTS:
            if self._reconnects >= MAX_RECONNECTS:
                log.error("Deepgram reconnect limit reached — transcription is down")
                self._closed = True
            return

        self._reconnects += 1
        # The read loop calls this on a drop, so it may be the running task —
        # cancelling it here would cancel ourselves mid-reconnect.
        current = asyncio.current_task()
        if self._reader is not None and self._reader is not current:
            self._reader.cancel()
        try:
            await self.connect()
            log.info("Deepgram reconnected (attempt %d)", self._reconnects)
        except Exception as exc:  # noqa: BLE001 — a failed retry must not end the call
            log.error("Deepgram reconnect failed: %s", exc)
            self._ws = None

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:  # noqa: BLE001 — teardown must never raise
                pass
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # -- internals ---------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                await self._dispatch(message)
            # A graceful close ends the iteration without raising, so falling
            # out of the loop is itself a disconnect we need to recover from.
            log.info("Deepgram stream ended")
        except websockets.ConnectionClosed:
            log.info("Deepgram connection closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("Deepgram read loop error: %s", exc)

        # Any exit that wasn't us shutting down means the bot has gone deaf.
        if not self._closed:
            self._ws = None
            await self._reconnect()

    async def _dispatch(self, message: dict) -> None:
        kind = message.get("type")

        if kind == "UtteranceEnd":
            if self._on_utterance_end:
                await self._on_utterance_end()
            return

        if kind != "Results":
            return

        alternatives = message.get("channel", {}).get("alternatives", [])
        if not alternatives:
            return
        text = (alternatives[0].get("transcript") or "").strip()
        if not text:
            return

        if message.get("is_final"):
            if self._on_final:
                await self._on_final(text)
            # speech_final means Deepgram detected an endpoint, not just a
            # finalized chunk — that's our cue that the agent stopped talking.
            if message.get("speech_final") and self._on_utterance_end:
                await self._on_utterance_end()
        elif self._on_partial:
            await self._on_partial(text)
