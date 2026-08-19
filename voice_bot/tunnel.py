"""Public HTTPS tunnel so Twilio can reach the local media server.

Three strategies, tried in order:

1. PUBLIC_URL from .env — you already have a public endpoint.
2. cloudflared quick tunnel — no account, no signup, and (unlike ngrok) not
   flagged as potentially-unwanted software by Windows Defender.
3. ngrok via pyngrok — the original path, kept as a fallback.

Windows Defender ships with PUA protection enabled, which quarantines
ngrok.exe on sight. Removing that requires local admin, which a lot of
machines don't have, so cloudflared is the default.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

QUICK_TUNNEL_URL = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
TUNNEL_TIMEOUT = 45.0

# winget installs shims here and it isn't always on PATH in an existing shell.
_EXTRA_PATHS = [
    Path.home() / "AppData/Local/Microsoft/WinGet/Links/cloudflared.exe",
    Path("C:/Program Files (x86)/cloudflared/cloudflared.exe"),
    Path("C:/Program Files/cloudflared/cloudflared.exe"),
]

Closer = Callable[[], None]


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    for candidate in _EXTRA_PATHS:
        if candidate.exists():
            return str(candidate)
    return None


def open_tunnel(port: int, *, public_url: str = "", ngrok_token: str = "") -> tuple[str, Closer]:
    """Return (https base url, shutdown callable)."""
    if public_url:
        url = public_url.rstrip("/")
        log.info("Using PUBLIC_URL from .env: %s", url)
        return url, lambda: None

    binary = find_cloudflared()
    if binary:
        try:
            return _cloudflared(binary, port)
        except RuntimeError as exc:
            log.warning("cloudflared failed (%s) — falling back to ngrok", exc)

    return _ngrok(port, ngrok_token)


def _cloudflared(binary: str, port: int) -> tuple[str, Closer]:
    log.info("Starting cloudflared quick tunnel...")
    process = subprocess.Popen(
        [
            binary, "tunnel",
            "--url", f"http://localhost:{port}",
            "--no-autoupdate",
            "--loglevel", "info",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    found: dict[str, str] = {}

    def watch() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            if "url" not in found:
                match = QUICK_TUNNEL_URL.search(line)
                if match:
                    found["url"] = match.group(0)
            if "failed to" in line.lower() or "error" in line.lower():
                log.debug("cloudflared: %s", line.rstrip())

    reader = threading.Thread(target=watch, daemon=True, name="cloudflared-reader")
    reader.start()

    deadline = time.monotonic() + TUNNEL_TIMEOUT
    while time.monotonic() < deadline:
        if "url" in found:
            url = found["url"]
            log.info("cloudflared tunnel: %s -> localhost:%d", url, port)
            # The edge needs a moment before it will route traffic.
            time.sleep(3)
            return url, lambda: _terminate(process)
        if process.poll() is not None:
            raise RuntimeError(f"cloudflared exited with code {process.returncode}")
        time.sleep(0.25)

    _terminate(process)
    raise RuntimeError(f"no tunnel URL within {TUNNEL_TIMEOUT:.0f}s")


def _ngrok(port: int, token: str) -> tuple[str, Closer]:
    try:
        from pyngrok import conf, ngrok
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("neither cloudflared nor pyngrok is available") from exc

    if token:
        conf.get_default().auth_token = token

    try:
        tunnel = ngrok.connect(port, "http", bind_tls=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 225:
            raise RuntimeError(
                "Windows Defender quarantined ngrok.exe (it is classified as a "
                "potentially-unwanted app). Install cloudflared instead:\n"
                "    winget install --id Cloudflare.cloudflared --scope user"
            ) from exc
        raise

    url = tunnel.public_url.replace("http://", "https://")
    log.info("ngrok tunnel: %s -> localhost:%d", url, port)
    return url, ngrok.kill


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
