"""Environment configuration.

Every secret lives in .env (gitignored). .env.example documents the shape.
Nothing in this module hardcodes a credential.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
RECORDINGS_DIR = ROOT / "recordings"
TRANSCRIPTS_DIR = ROOT / "transcripts"

load_dotenv(ROOT / ".env")


class ConfigError(RuntimeError):
    """A required environment variable is missing."""


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required environment variable {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _optional(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


@dataclass(frozen=True)
class Config:
    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str
    target_phone_number: str

    # ElevenLabs
    elevenlabs_api_key: str
    elevenlabs_voice_female: str
    elevenlabs_voice_male: str
    elevenlabs_model: str

    # Deepgram
    deepgram_api_key: str
    deepgram_model: str

    # Anthropic
    anthropic_api_key: str
    claude_model: str

    # Plumbing
    ngrok_auth_token: str
    public_url: str
    server_port: int
    max_call_seconds: int
    gap_between_calls: int

    def voice_id(self, gender: str) -> str:
        return self.elevenlabs_voice_male if gender == "male" else self.elevenlabs_voice_female


def load_config() -> Config:
    cfg = Config(
        twilio_account_sid=_require("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_require("TWILIO_AUTH_TOKEN"),
        twilio_phone_number=_require("TWILIO_PHONE_NUMBER"),
        target_phone_number=_optional("TARGET_PHONE_NUMBER", "+18054398008"),
        elevenlabs_api_key=_require("ELEVENLABS_API_KEY"),
        elevenlabs_voice_female=_require("ELEVENLABS_VOICE_ID_FEMALE"),
        elevenlabs_voice_male=_require("ELEVENLABS_VOICE_ID_MALE"),
        elevenlabs_model=_optional("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
        deepgram_api_key=_require("DEEPGRAM_API_KEY"),
        deepgram_model=_optional("DEEPGRAM_MODEL", "nova-2-phonecall"),
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        claude_model=_optional("CLAUDE_MODEL", "claude-sonnet-4-6"),
        ngrok_auth_token=os.getenv("NGROK_AUTH_TOKEN", "").strip(),
        public_url=os.getenv("PUBLIC_URL", "").strip(),
        server_port=int(_optional("SERVER_PORT", "5050")),
        max_call_seconds=int(_optional("MAX_CALL_SECONDS", "480")),
        gap_between_calls=int(_optional("GAP_BETWEEN_CALLS", "60")),
    )

    # Hard safety rail: this bot is only ever allowed to dial the assessment line.
    if cfg.target_phone_number != "+18054398008":
        raise ConfigError(
            "TARGET_PHONE_NUMBER must be +18054398008 — this bot only calls the assessment line."
        )

    RECORDINGS_DIR.mkdir(exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    return cfg
