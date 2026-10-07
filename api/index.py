"""Discord interaction gateway for Vercel.

Vercel handles the public Discord endpoint and forwards verified interactions
to the existing hosted Movie-Bot worker on Render. The Discord Gateway,
voice connection, FFmpeg, and long-running media work remain in the bot
process.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


def _headers(request: BaseHTTPRequestHandler) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in request.headers.items()}


def _verify_discord_signature(body: bytes, headers: dict[str, str]) -> bool:
    public_key = os.getenv("DISCORD_PUBLIC_KEY", "").strip()
    signature = headers.get("x-signature-ed25519", "")
    timestamp = headers.get("x-signature-timestamp", "")

    if not public_key or not signature or not timestamp:
        return False

    try:
        key = VerifyKey(bytes.fromhex(public_key))
        key.verify(timestamp.encode("utf-8") + body, bytes.fromhex(signature))
        return True
    except (ValueError, BadSignatureError):
        return False


def _discord_ack(payload: dict[str, Any]) -> dict[str, Any]:
    interaction_type = int(payload.get("type", 0))

    if interaction_type == 1:  # Discord PING
        return {"type": 1}

    if interaction_type == 2:  # slash command
        return {"type": 5, "data": {"flags": 64}}

    if interaction_type == 3:  # component/button/select interaction
        return {"type": 6}

    if interaction_type == 4:  # autocomplete
        return {"type": 8, "data": {"choices": []}}

    if interaction_type == 5:  # modal submit
        return {"type": 5, "data": {"flags": 64}}

    return {"type": 5, "data": {"flags": 64}}


def _forward_to_bot(payload: dict[str, Any], secret: str) -> None:
    target = os.getenv("BOT_INTERACTION_URL", "").strip()
    if not target:
        return

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        target,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Movie-Bot-Secret": secret,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=2.5) as response:
            response.read(64)
    except (urllib.error.URLError, TimeoutError, OSError):
        # Discord still needs its acknowledgement within three seconds.
        # Render may continue processing the request after this short timeout.
        pass


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        self._send_json(
            200,
            {
                "ok": True,
                "service": "Movie Bot",
                "runtime": "Vercel",
                "domain": "moviebot.devs.surf",
            },
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        headers = _headers(self)

        if not _verify_discord_signature(body, headers):
            self._send_json(401, {"error": "Invalid Discord signature"})
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Invalid JSON"})
            return

        if int(payload.get("type", 0)) == 1:
            self._send_json(200, {"type": 1})
            return

        # Forward the already verified interaction to the existing bot.
        _forward_to_bot(payload, os.getenv("WAKE_SECRET", "").strip())

        self._send_json(200, _discord_ack(payload))

    def log_message(self, format: str, *args: Any) -> None:
        return


# Keep aliases for Vercel's Python entry-point detector.
app = handler
application = handler
