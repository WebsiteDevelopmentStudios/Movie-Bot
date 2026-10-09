import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

logger = logging.getLogger("movie-bot.youtube")


def _youtube_cookies_file() -> str | None:
    # Match main.py's import destination when no explicit environment path is
    # configured. Previously, cookies were saved to this default path but the
    # extractor ignored them unless YOUTUBE_COOKIES_FILE was also set.
    value = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
    path = (
        Path(value).expanduser()
        if value
        else Path(__file__).resolve().parent / "youtube-cookies.txt"
    )
    if not path.is_file():
        if value:
            logger.warning("YOUTUBE_COOKIES_FILE is set but the file does not exist: %s", path)
        else:
            logger.info("No YouTube cookies file found at the default path: %s", path)
        return None
    return str(path)


def _is_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme in {"http", "https"}
        and host in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "www.youtu.be",
        }
    )


def _extract_sync(query: str) -> dict | None:
    target = query.strip()
    if not target:
        return None

    if not _is_youtube_url(target):
        target = f"ytsearch1:{target}"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
        },
    }

    # bgutil-ytdlp-pot-provider is the preferred PO-token backend. The
    # provider runs separately from the bot so this container does not need
    # Chromium, Node.js, or browser automation.
    pot_provider = os.getenv(
        "YOUTUBE_POT_PROVIDER_URL",
        "https://movie-bot-pot-provider.onrender.com",
    ).strip().rstrip("/")
    if pot_provider:
        opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {
                "base_url": pot_provider,
            },
            # These clients are recommended by the provider documentation when
            # a PO-token flow needs to be explicitly selected.
            "youtube": {
                "player_client": ["mweb", "tv", "web_safari"],
            },
        }

    cookies = _youtube_cookies_file()
    if cookies:
        opts["cookiefile"] = cookies

    proxy = os.getenv("YOUTUBE_PROXY", "").strip()
    if proxy:
        opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
        if not info:
            return None

        if "entries" in info:
            entries = [entry for entry in (info.get("entries") or []) if entry]
            info = entries[0] if entries else None
        if not info:
            return None

        formats = info.get("formats") or []
        audio_formats = [
            item
            for item in formats
            if item.get("url")
            and item.get("vcodec") in {None, "none"}
            and item.get("acodec") not in {None, "none"}
            and str(item.get("protocol", "")).startswith(("http", "https"))
        ]

        chosen = audio_formats[-1] if audio_formats else None
        direct_url = chosen.get("url") if chosen else info.get("url")
        if not direct_url:
            return None

        duration_ms = 0
        if info.get("duration"):
            duration_ms = max(0, int(float(info["duration"]) * 1000))

        artist = (
            str(
                info.get("artist")
                or info.get("uploader")
                or info.get("channel")
                or "Unknown Artist"
            ).strip()
        )

        return {
            "url": str(direct_url),
            "title": str(info.get("title") or "Unknown Title").strip(),
            "artist": artist or "Unknown Artist",
            "duration_ms": duration_ms,
            "webpage_url": info.get("webpage_url") or info.get("original_url"),
        }


async def extract_youtube_audio(query: str) -> dict | None:
    """Extract a playable direct audio URL without asking Lavalink to resolve YouTube.

    yt-dlp runs off the event loop because extraction performs blocking HTTP and
    JavaScript/player parsing work. YouTube authentication/cookies stay inside
    the extractor environment and are never sent to Discord or Lavalink.
    """
    try:
        return await asyncio.to_thread(_extract_sync, query)
    except yt_dlp.utils.DownloadError as exc:
        logger.warning("yt-dlp could not extract %r: %s", query, exc)
        return None
    except Exception as exc:
        logger.exception("Unexpected YouTube extraction error for %r: %s", query, exc)
        return None
