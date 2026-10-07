"""Hosted bridge for Movie-Bot.

The Cloudflare Worker receives Discord interactions and forwards them here.
This service runs the existing discord.py bot and proxies the existing movie
web server, so the local PC is no longer required.

Render exposes this bridge on its public PORT. The existing movie web server
continues to run internally on MOVIE_PORT (8080 by default).
"""
import asyncio
import os
from typing import Any

render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
if render_url:
    os.environ["PUBLIC_BASE_URL"] = render_url
os.environ.setdefault("MOVIE_HOST", "127.0.0.1")
os.environ.setdefault("MOVIE_PORT", "8080")

import aiohttp
from aiohttp import web
import discord

import main as movie_bot


INTERNAL_MOVIE_URL = f"http://127.0.0.1:{os.getenv('MOVIE_PORT', '8080')}"
BRIDGE_HOST = "0.0.0.0"
BRIDGE_PORT = int(os.getenv("PORT", "10000"))
WAKE_SECRET = os.getenv("WAKE_SECRET", "").strip()


class BridgeInteractionResponse:
    """Route discord.py response calls to the already-deferred Worker response."""

    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction

    def is_done(self) -> bool:
        return True

    async def send_message(self, content=None, **kwargs):
        return await self.interaction.followup.send(content=content, wait=True, **kwargs)

    async def defer(self, **kwargs):
        return None

    async def edit_message(self, **kwargs):
        message = self.interaction.message
        if message is None:
            return await self.interaction.edit_original_response(**kwargs)
        return await message.edit(**kwargs)

    async def send_modal(self, *args, **kwargs):
        raise RuntimeError("Modals are not supported through the interaction bridge yet.")

    async def autocomplete(self, *args, **kwargs):
        raise RuntimeError("Autocomplete is not supported through the interaction bridge yet.")


class BridgedInteraction(discord.Interaction):
    """discord.Interaction with a writable-looking response backed by our bridge."""

    def __init__(self, data: dict[str, Any], state: Any):
        super().__init__(data=data, state=state)
        self._bridge_response = BridgeInteractionResponse(self)

    @property
    def response(self) -> BridgeInteractionResponse:
        return self._bridge_response


async def discord_interaction(request: web.Request) -> web.Response:
    if WAKE_SECRET and request.headers.get("X-Movie-Bot-Secret", "") != WAKE_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        payload: dict[str, Any] = await request.json()
    except (ValueError, aiohttp.ContentTypeError):
        return web.json_response({"error": "invalid JSON"}, status=400)

    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("token"):
        return web.json_response({"error": "invalid Discord interaction"}, status=400)

    try:
        interaction = BridgedInteraction(data=payload, state=movie_bot.bot._connection)
        await movie_bot.bot.tree._from_interaction(interaction)
    except Exception:
        movie_bot.logger.exception("Failed to dispatch bridged Discord interaction")
        return web.json_response({"error": "interaction dispatch failed"}, status=500)

    return web.json_response({"ok": True})


async def health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "bot_ready": movie_bot.bot.is_ready(),
            "architecture": "cloudflare-worker-render-wake",
        }
    )


async def proxy_movie_server(request: web.Request) -> web.StreamResponse:
    """Stream the existing movie web server through Render's public port."""
    target = f"{INTERNAL_MOVIE_URL}{request.rel_url}"

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }

    body = None
    if request.method not in {"GET", "HEAD"}:
        body = await request.read()

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.request(
                request.method,
                target,
                headers=headers,
                data=body,
                allow_redirects=False,
            ) as upstream:
                response_headers = {
                    key: value
                    for key, value in upstream.headers.items()
                    if key.lower() not in {"transfer-encoding", "connection", "keep-alive"}
                }

                response = web.StreamResponse(
                    status=upstream.status,
                    headers=response_headers,
                )
                await response.prepare(request)

                if request.method != "HEAD":
                    async for chunk in upstream.content.iter_chunked(1024 * 1024):
                        await response.write(chunk)

                await response.write_eof()
                return response
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            movie_bot.logger.warning("Movie proxy failed: %s", exc)
            return web.json_response(
                {"error": "movie service is starting or unavailable"},
                status=503,
            )


async def start_bridge() -> None:
    app = web.Application(client_max_size=1024 * 1024 * 1024 * 2)
    app.router.add_get("/health", health)
    app.router.add_post("/discord/interactions", discord_interaction)
    app.router.add_route("*", "/movie/{tail:.*}", proxy_movie_server)
    app.router.add_route("*", "/media/{tail:.*}", proxy_movie_server)
    app.router.add_route("*", "/hls/{tail:.*}", proxy_movie_server)
    app.router.add_route("*", "/parts/{tail:.*}", proxy_movie_server)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BRIDGE_HOST, BRIDGE_PORT)
    await site.start()

    movie_bot.logger.info(
        "Cloudflare/Render interaction bridge listening on %s:%s",
        BRIDGE_HOST,
        BRIDGE_PORT,
    )

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


async def run() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_TOKEN is missing.")

    await asyncio.gather(
        movie_bot.bot.start(token),
        start_bridge(),
    )


if __name__ == "__main__":
    asyncio.run(run())
