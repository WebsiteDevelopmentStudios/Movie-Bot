"""Discord interaction endpoint for Vercel."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


def verify_discord_request(body: bytes, signature: str, timestamp: str) -> bool:
    public_key = os.environ.get("DISCORD_PUBLIC_KEY", "").strip()
    if not public_key or not signature or not timestamp:
        return False

    try:
        public_key_bytes = bytes.fromhex(public_key)
        signature_bytes = bytes.fromhex(signature)
        if len(public_key_bytes) != 32 or len(signature_bytes) != 64:
            return False
        VerifyKey(public_key_bytes).verify(
            timestamp.encode("utf-8") + body,
            signature_bytes,
        )
        return True
    except (ValueError, TypeError, BadSignatureError):
        return False


def forward_interaction(payload: dict[str, Any]) -> None:
    target = os.environ.get("BOT_INTERACTION_URL", "").strip()
    secret = os.environ.get("WAKE_SECRET", "").strip()
    if not target or not secret:
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
        pass


def interaction_response(payload: dict[str, Any]) -> dict[str, Any]:
    interaction_type = int(payload.get("type", 0))
    if interaction_type == 1:
        return {"type": 1}
    if interaction_type == 2:
        return {"type": 5, "data": {"flags": 64}}
    if interaction_type == 3:
        return {"type": 6}
    if interaction_type == 4:
        return {"type": 8, "data": {"choices": []}}
    if interaction_type == 5:
        return {"type": 5, "data": {"flags": 64}}
    return {"type": 5, "data": {"flags": 64}}


class handler(BaseHTTPRequestHandler):
    def _json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._json(200, {
            "ok": True,
            "service": "Movie Bot",
            "runtime": "Vercel",
            "endpoint": "Discord interactions",
        })

    def do_POST(self) -> None:
        content_length = self.headers.get("Content-Length")
        try:
            length = int(content_length) if content_length else 0
        except ValueError:
            self._json(400, {"error": "Invalid Content-Length"})
            return

        body = self.rfile.read(length)
        signature = self.headers.get("X-Signature-Ed25519", "").strip()
        timestamp = self.headers.get("X-Signature-Timestamp", "").strip()

        if not verify_discord_request(body, signature, timestamp):
            self._json(401, {"error": "Invalid Discord signature"})
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "Invalid JSON"})
            return

        if int(payload.get("type", 0)) == 1:
            self._json(200, {"type": 1})
            return

        forward_interaction(payload)
        self._json(200, interaction_response(payload))

    def log_message(self, format: str, *args: Any) -> None:
        return


app = handler
application = handler
