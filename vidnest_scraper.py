"""vidnest_scraper.py — resolve playable HLS playlists from VidNest movie pages.

Fetching strategy:
  - If curl_cffi is installed, pages/embeds/assets are fetched with a real
    Chrome TLS fingerprint, which passes Cloudflare's passive checks.
  - Otherwise aiohttp is used (may be blocked by Cloudflare with HTTP 403).

Resolution strategies, in order:
  1. Regex the movie page HTML for playlist URLs.
  2. Follow iframe embeds on the page and scan their HTML and script assets.
  3. Validate every candidate by requesting it and checking for #EXTM3U.

Note: your bot only downloads what these pages expose; make sure any content
you stream is content you're allowed to stream.
"""

import asyncio
import logging
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import aiohttp

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

logger = logging.getLogger("movie-bot.vidnest")

VIDNEST_HOSTS = {"vidnest.fun", "www.vidnest.fun"}
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

ABSOLUTE_M3U8_RE = re.compile(
    r"""https?://[^\s"'<>\\]+?\.m3u8(?:\?[^\s"'<>\\]*)?""", re.IGNORECASE
)
QUOTED_M3U8_RE = re.compile(
    r"""["']([^"']+?\.m3u8(?:\?[^"']*)?)["']""", re.IGNORECASE
)
IFRAME_SRC_RE = re.compile(
    r"""<iframe[^>]+?src=["']([^"']+)["']""", re.IGNORECASE
)
SCRIPT_SRC_RE = re.compile(
    r"""<script[^>]+?src=["']([^"']+)["']""", re.IGNORECASE
)

MAX_ASSETS = 20
MAX_EMBEDS = 5
PAGE_TIMEOUT_SECONDS = 30


def is_vidnest_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in VIDNEST_HOSTS
        and "/movie/" in parsed.path
    )


def _normalize_text(text: str) -> str:
    """Undo common HTML/JS escaping so regexes can match real URLs."""
    text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    return unescape(text)


def _find_candidates(text: str, base_url: str) -> list[str]:
    """Return absolute playlist URLs present in `text`, deduplicated."""
    text = _normalize_text(text)
    candidates: list[str] = []

    for match in ABSOLUTE_M3U8_RE.finditer(text):
        candidates.append(match.group(0))
    for match in QUOTED_M3U8_RE.finditer(text):
        candidates.append(match.group(1))

    seen: set[str] = set()
    absolute: list[str] = []
    for candidate in candidates:
        candidate = candidate.rstrip("),;]'\"")
        full = urljoin(base_url, candidate)
        if full not in seen:
            seen.add(full)
            absolute.append(full)
    return absolute


def _fetch_with_curl_cffi(url: str, timeout: int) -> str | None:
    """Blocking fetch with a Chrome TLS fingerprint. Runs in a thread."""
    try:
        response = curl_requests.get(
            url,
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
        )
        if response.status_code != 200:
            logger.warning("Browser fetch %s -> HTTP %s", url, response.status_code)
            return None
        return response.text
    except Exception as exc:
        logger.warning("Browser fetch %s failed: %s", url, exc)
        return None


async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str | None:
    """Fetch page/asset text, preferring curl_cffi when available."""
    if curl_requests is not None:
        return await asyncio.to_thread(_fetch_with_curl_cffi, url, PAGE_TIMEOUT_SECONDS)

    try:
        async with session.get(url, allow_redirects=True) as response:
            if response.status != 200:
                logger.warning("Fetch %s -> HTTP %s", url, response.status)
                return None
            return await response.text(errors="replace")
    except aiohttp.ClientResponseError as exc:
        logger.warning("Fetch %s failed: HTTP %s", url, exc.status)
        return None
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        logger.warning("Fetch %s failed: %s", url, exc)
        return None


async def _validate_playlist(session: aiohttp.ClientSession, url: str) -> str | None:
    """Confirm a candidate URL actually serves an M3U8 playlist."""
    if curl_requests is not None:
        def check_with_curl() -> bool:
            try:
                response = curl_requests.get(
                    url, impersonate="chrome", timeout=20,
                )
                if response.status_code != 200:
                    return False
                return "#EXTM3U" in response.text[:4096]
            except Exception:
                return False

        if await asyncio.to_thread(check_with_curl):
            return url
        return None

    try:
        async with session.get(url, allow_redirects=True) as response:
            if response.status != 200:
                return None
            content_type = response.headers.get("Content-Type", "").lower()
            if "mpegurl" in content_type:
                return url
            # Stream only the first chunk; we just need the #EXTM3U header.
            body = b""
            async for chunk in response.content.iter_any():
                body += chunk
                if b"#EXTM3U" in body or len(body) > 4096:
                    break
            if b"#EXTM3U" in body:
                return url
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return None
    return None


async def resolve_vidnest_playlist(movie_url: str, progress=None) -> str | None:
    """Resolve a playable HLS playlist URL for a VidNest movie page.

    Returns the absolute playlist URL, or None when nothing playable was found.
    Raises ValueError for non-VidNest input and RuntimeError for hostile or
    dead upstream responses.
    """
    movie_url = movie_url.strip()
    if not is_vidnest_url(movie_url):
        raise ValueError("Provide a VidNest movie page (https://vidnest.fun/movie/<tmdb_id>).")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 90

    timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=15)
    async with aiohttp.ClientSession(timeout=timeout, headers=BROWSER_HEADERS) as session:
        if progress:
            await progress("Loading the VidNest movie page...")

        page_html = await _fetch_text(session, movie_url)
        if page_html is None:
            raise RuntimeError(
                "VidNest did not respond (check the bot log for the exact HTTP status). "
                "The page may be down, missing, or blocking the bot."
            )

        # Guard against Cloudflare serving an interstitial page that returns
        # HTTP 200 but contains no playlists and no player markup.
        if "Just a moment" in page_html or "cf-challenge" in page_html:
            raise RuntimeError(
                "VidNest is showing a Cloudflare challenge page. "
                "The bot could not pass it automatically."
            )

        # Strategy 1: playlists plainly in the movie page.
        if progress:
            await progress("Scanning the movie page for streams...")
        for candidate in _find_candidates(page_html, movie_url):
            validated = await _validate_playlist(session, candidate)
            if validated:
                return validated

        # Strategy 2+3: follow embeds, then their script assets.
        embed_urls = [
            urljoin(movie_url, src)
            for src in IFRAME_SRC_RE.findall(page_html)
        ][:MAX_EMBEDS]

        for embed_index, embed_url in enumerate(embed_urls, 1):
            if loop.time() > deadline:
                break
            if progress:
                await progress(f"Checking player embed {embed_index}/{len(embed_urls)}...")

            embed_html = await _fetch_text(session, embed_url)
            if embed_html is None:
                continue

            for candidate in _find_candidates(embed_html, embed_url):
                validated = await _validate_playlist(session, candidate)
                if validated:
                    return validated

            asset_urls = [
                urljoin(embed_url, src)
                for src in SCRIPT_SRC_RE.findall(embed_html)
            ][:MAX_ASSETS]

            for asset_url in asset_urls:
                if loop.time() > deadline:
                    break
                asset_text = await _fetch_text(session, asset_url)
                if asset_text is None:
                    continue
                for candidate in _find_candidates(asset_text, embed_url):
                    validated = await _validate_playlist(session, candidate)
                    if validated:
                        logger.info("VidNest playlist found in embed asset: %s", asset_url)
                        return validated

        # Also scan the movie page's own scripts (sometimes the player is inline).
        for asset_url in [
            urljoin(movie_url, src) for src in SCRIPT_SRC_RE.findall(page_html)
        ][:MAX_ASSETS]:
            if loop.time() > deadline:
                break
            asset_text = await _fetch_text(session, asset_url)
            if asset_text is None:
                continue
            for candidate in _find_candidates(asset_text, movie_url):
                validated = await _validate_playlist(session, candidate)
                if validated:
                    return validated

    return None
