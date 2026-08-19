"""List the ElevenLabs voices this API key can actually use.

    python -m voice_bot.voices

Free plans reject *library* and *professional* voices with HTTP 402. That
failure is easy to miss because it makes the bot silent rather than crashing:
every TTS call fails, we send no audio, and the far end hangs up on what it
thinks is dead air. Run this before a batch to confirm the configured voices
are usable.
"""

from __future__ import annotations

import sys

import httpx

from .config import load_config

VOICES_URL = "https://api.elevenlabs.io/v1/voices"
TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
PROBE_TEXT = "Hi, um, I'm hoping to make an appointment."


def main() -> int:
    config = load_config()
    headers = {"xi-api-key": config.elevenlabs_api_key}

    try:
        response = httpx.get(VOICES_URL, headers=headers, timeout=60.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Could not list voices: {exc}")
        return 1

    voices = response.json().get("voices", [])
    print(f"{len(voices)} voices visible to this key\n")
    print(f"{'name':<40}{'gender':<10}{'category':<14}voice_id")
    print("-" * 96)
    for voice in sorted(voices, key=lambda v: (v.get("category", ""), v.get("name", ""))):
        labels = voice.get("labels") or {}
        name = voice.get("name", "")[:38]
        category = voice.get("category", "")
        marker = "" if category == "premade" else "   <- needs a paid plan"
        print(f"{name:<40}{labels.get('gender', ''):<10}{category:<14}"
              f"{voice.get('voice_id', '')}{marker}")

    print("\nOnly `premade` voices work on a free plan.\n")
    print("Checking the voices this project is configured to use:")

    ok = True
    for label, voice_id in (
        ("female", config.elevenlabs_voice_female),
        ("male", config.elevenlabs_voice_male),
    ):
        try:
            probe = httpx.post(
                TTS_URL.format(voice_id=voice_id),
                params={"output_format": "ulaw_8000"},
                headers={**headers, "content-type": "application/json"},
                json={"text": PROBE_TEXT, "model_id": config.elevenlabs_model},
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            print(f"  {label:<8}{voice_id}  request failed: {exc}")
            ok = False
            continue

        if probe.status_code == 200:
            seconds = len(probe.content) / 8000
            print(f"  {label:<8}{voice_id}  OK — {seconds:.1f}s of audio")
        else:
            ok = False
            detail = probe.json().get("detail", {}) if probe.text.startswith("{") else {}
            message = detail.get("message", probe.text[:200])
            print(f"  {label:<8}{voice_id}  HTTP {probe.status_code} — {message}")

    if not ok:
        print("\nAt least one configured voice is unusable. Pick a `premade` voice_id "
              "from the list above and set it in .env.")
        return 1

    print("\nBoth voices work. Safe to run calls.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
