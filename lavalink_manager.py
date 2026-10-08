import asyncio
import logging
import os
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger("movie-bot.lavalink")

# External Lavalink configuration.
# The Python bot does NOT download Java or start a JVM.
LAVALINK_URI = os.getenv("LAVALINK_URI", "").strip().rstrip("/")
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "").strip()
LAVALINK_HOST = os.getenv("LAVALINK_HOST", "").strip()
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2333"))

# Backwards-compatible defaults for code that imports these names.
if not LAVALINK_URI and LAVALINK_HOST:
    LAVALINK_URI = f"http://{LAVALINK_HOST}:{LAVALINK_PORT}"


def _validate_lavalink_config() -> None:
    if not LAVALINK_URI:
        raise RuntimeError(
            "External Lavalink is not configured. Set LAVALINK_URI and "
            "LAVALINK_PASSWORD in the hosting environment."
        )

    parsed = urlparse(LAVALINK_URI)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(
            "LAVALINK_URI must be an HTTP(S) Lavalink address such as "
            "http://your-lavalink-host:2333."
        )

    if not LAVALINK_PASSWORD:
        raise RuntimeError(
            "LAVALINK_PASSWORD is not configured. Set it to the password "
            "configured on the external Lavalink server."
        )


async def start_lavalink(password: str | None = None) -> None:
    """Validate external Lavalink configuration.

    Lavalink is intentionally NOT started here. This bot runs in a
    Python-only container, so Java/Lavalink must run in a separate service.
    """
    _validate_lavalink_config()
    logger.info("Using external Lavalink at %s.", LAVALINK_URI)


async def wait_until_ready(timeout: float = 120.0) -> None:
    """Wait until the external Lavalink HTTP endpoint responds."""
    _validate_lavalink_config()
    deadline = asyncio.get_running_loop().time() + timeout
    version_url = f"{LAVALINK_URI}/version"

    headers = {
        "Authorization": LAVALINK_PASSWORD,
        "User-Agent": "Movie-Bot/1.0",
    }

    last_error: Exception | None = None
    timeout_value = aiohttp.ClientTimeout(total=5)

    async with aiohttp.ClientSession(timeout=timeout_value) as session:
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with session.get(version_url, headers=headers) as response:
                    if response.status == 200:
                        version = (await response.text()).strip()
                        logger.info("External Lavalink is ready (version %s).", version)
                        return

                    body = (await response.text())[:200]
                    last_error = RuntimeError(
                        f"Lavalink returned HTTP {response.status}: {body}"
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc

            await asyncio.sleep(1)

    raise TimeoutError(
        f"Timed out waiting for external Lavalink at {LAVALINK_URI}/version"
        + (f": {last_error}" if last_error else "")
    )


async def keep_lavalink_awake(interval: float = 300.0) -> None:
    """Keep a Render free web service awake while the bot is running.

    Render can suspend an idle free web service. The Discord bot periodically
    requests Lavalink's authenticated /version endpoint so the Lavalink
    service receives real inbound traffic while the bot is online.
    """
    _validate_lavalink_config()

    headers = {
        "Authorization": LAVALINK_PASSWORD,
        "User-Agent": "Movie-Bot/1.0",
    }
    timeout = aiohttp.ClientTimeout(total=10)
    version_url = f"{LAVALINK_URI}/version"

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(version_url, headers=headers) as response:
                    if response.status == 200:
                        logger.debug("Lavalink keep-alive request succeeded.")
                    else:
                        logger.warning(
                            "Lavalink keep-alive returned HTTP %s.",
                            response.status,
                        )
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                logger.debug("Lavalink keep-alive failed: %s", exc)

            await asyncio.sleep(interval)


async def stop_lavalink() -> None:
    """Nothing to stop because Lavalink runs as a separate service."""
    return
