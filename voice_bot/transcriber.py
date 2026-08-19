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
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(ulaw)
        except websockets.ConnectionClosed:
            self._closed = True

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
        except websockets.ConnectionClosed:
            log.info("Deepgram connection closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("Deepgram read loop error: %s", exc)

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
