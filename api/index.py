"""Vercel HTTP entry point for Movie-Bot.

This endpoint intentionally does not start the Discord Gateway bot. Vercel
executes this function on demand; the long-running Discord bot remains a
separate process until the architecture is moved to a Vercel-compatible
runtime.
"""

from __future__ import annotations

import json
import os
from typing import Any


def handler(request: Any) -> dict[str, Any]:
    method = getattr(request, "method", "GET")

    if method == "GET":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "ok": True,
                    "service": "Movie Bot",
                    "runtime": "Vercel",
                }
            ),
        }

    if method != "POST":
        return {
            "statusCode": 405,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Method not allowed"}),
        }

    # Keep the endpoint deliberately small. Discord interaction verification
    # and dispatch will be wired here as the Vercel architecture is migrated.
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"ok": True}),
    }
