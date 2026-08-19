"""The patient's brain: Claude turns the agent's words into the next thing to say.

Conversation mapping is deliberately inverted from what you'd expect: the
*agent* we are testing occupies the `user` role, and our bot is the `assistant`.
That is the natural framing for "given what they just said, what do I say next".
"""

from __future__ import annotations

import logging
import re

from anthropic import AsyncAnthropic, APIError

from .scenarios import Scenario

log = logging.getLogger(__name__)

END_CALL_TOKEN = "[END_CALL]"

# Things Claude should never actually speak aloud, even if it emits them.
_STAGE_DIRECTIONS = re.compile(r"[\*_`]|\((?:pause|sighs?|laughs?|quietly)[^)]*\)", re.IGNORECASE)


class PatientBrain:
    """Generates the caller's next utterance."""

    def __init__(self, api_key: str, model: str, scenario: Scenario) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._scenario = scenario
        self._system = scenario.system_prompt()
        self._history: list[dict] = []
        self.should_hang_up = False

    @property
    def history(self) -> list[dict]:
        return self._history

    def record_agent(self, text: str) -> None:
        """Log what the agent said so it becomes context for the next turn."""
        text = text.strip()
        if not text:
            return
        if self._history and self._history[-1]["role"] == "user":
            self._history[-1]["content"] += " " + text
        else:
            self._history.append({"role": "user", "content": text})

    def record_self(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._history and self._history[-1]["role"] == "assistant":
            self._history[-1]["content"] += " " + text
        else:
            self._history.append({"role": "assistant", "content": text})

    async def next_line(self, nudge: str | None = None) -> str:
        """Ask Claude for the caller's next line. Returns '' if there's nothing to say."""
        messages = list(self._history)

        # Claude requires the conversation to start with a user turn. If the
        # agent hasn't said anything yet, seed it with the call context.
        if not messages or messages[0]["role"] != "user":
            messages.insert(0, {
                "role": "user",
                "content": "[The line connects. You hear the practice's phone system pick up.]",
            })

        if nudge:
            messages.append({"role": "user", "content": f"[{nudge}]"})

        try:
            response = await self._client.messages.create(
                model=self._model,
                # A hard ceiling on length. Phone turns are one or two short
                # sentences; give it room for a paragraph and it writes one.
                max_tokens=110,
                system=self._system,
                messages=messages,
            )
        except APIError as exc:
            log.error("Claude call failed: %s", exc)
            return ""

        raw = "".join(block.text for block in response.content if block.type == "text").strip()
        return self._clean(raw)

    def _clean(self, text: str) -> str:
        if END_CALL_TOKEN in text:
            self.should_hang_up = True
            text = text.replace(END_CALL_TOKEN, "")
        text = _STAGE_DIRECTIONS.sub("", text)
        # Collapse whitespace but keep sentence structure for the TTS.
        return re.sub(r"\s+", " ", text).strip()

    async def aclose(self) -> None:
        await self._client.close()
