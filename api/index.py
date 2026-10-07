"""Vercel HTTP entry point for Movie-Bot."""

from __future__ import annotations

import json
from typing import Any


def handler(request: Any) -> dict[str, Any]:
    method = getattr(request, "method", "GET")

    if method == "GET":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "ok": True,
                "service": "Movie Bot",
                "runtime": "Vercel",
            }),
        }

    if method != "POST":
        return {
            "statusCode": 405,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Method not allowed"}),
        }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"ok": True}),
    }


# Explicit aliases for Vercel's Python entry-point detection.
app = handler
application = handler
