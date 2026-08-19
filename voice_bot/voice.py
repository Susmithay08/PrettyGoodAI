"""ElevenLabs text-to-speech.

We ask ElevenLabs for `ulaw_8000` directly, which is exactly the format Twilio
Media Streams wants — no resampling or transcoding in our hot path.
"""

from __future__ import annotations

import logging

import httpx

from .audio import apply_gain

log = logging.getLogger(__name__)

TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"


class Speaker:
    """Turns text into Twilio-ready μ-law audio."""

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model: str = "eleven_turbo_v2_5",
        gain: float = 1.0,
        stability: float = 0.45,
        similarity_boost: float = 0.75,
        style: float = 0.35,
        speed: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._gain = gain
        self._settings = {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": True,
            "speed": speed,
        }
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def synthesize(self, text: str) -> bytes:
        """Return 8 kHz μ-law audio for `text`. Empty bytes on failure."""
        text = text.strip()
        if not text:
            return b""

        try:
            response = await self._client.post(
                TTS_URL.format(voice_id=self._voice_id),
                params={"output_format": "ulaw_8000", "optimize_streaming_latency": "3"},
                headers={
                    "xi-api-key": self._api_key,
                    "accept": "audio/basic",
                    "content-type": "application/json",
                },
                json={
                    "text": text,
                    "model_id": self._model,
                    "voice_settings": self._settings,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.error("ElevenLabs %s: %s", exc.response.status_code, exc.response.text[:300])
            return b""
        except httpx.HTTPError as exc:
            log.error("ElevenLabs request failed: %s", exc)
            return b""

        return apply_gain(response.content, self._gain)

    async def aclose(self) -> None:
        await self._client.aclose()
