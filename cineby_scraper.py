"""cineby_scraper.py — resolve playable HLS playlists from Cineby movie pages.

Cineby watch pages are React apps; the m3u8 often appears only after the
player initializes. Without a browser we try, in order:
  1. Scan the watch page HTML for playlist URLs (plain HTML case).
  2. Follow iframe embeds and scan their HTML and script assets.
  3. Ask yt-dlp (with browser impersonation) to resolve the page directly —
     yt-dlp handles many provider embeds natively.

All requests use a real Chrome TLS fingerprint via curl_cffi when available.
Only content you are authorized to stream should be played back.
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

logger = logging.getLogger("movie-bot.cineby")

CINEBY_HOSTS = {"cineby.tech", "www.cineby.tech"}
CINEBY_REFERER = "https://cineby.tech/"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
)

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

MAX_ASSETS = 15
MAX_EMBEDS = 5


def is_cineby_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in CINEBY_HOSTS
        and "/movie/" in parsed.path
    )


def cineby_tmdb_id(value: str) -> str | None:
    match = re.search(r"/movie/(\d+)", value)
    return match.group(1) if match else None


def _normalize_text(text: str) -> str:
    text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    return unescape(text)


def _find_candidates(text: str, base_url: str) -> list[str]:
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


def _fetch_sync(url: str, referer: str) -> str | None:
    try:
        if curl_requests is not None:
            response = curl_requests.get(
                url,
                impersonate="chrome",
                timeout=25,
                headers={"Referer": referer},
            )
        else:
            return None
        if response.status_code != 200:
            logger.warning("Cineby fetch %s -> HTTP %s", url, response.status_code)
            return None
        return response.text
    except Exception as exc:
        logger.warning("Cineby fetch %s failed: %s", url, exc)
        return None


async def _fetch_text(url: str, referer: str = CINEBY_REFERER) -> str | None:
    if curl_requests is not None:
        return await asyncio.to_thread(_fetch_sync, url, referer)
    # aiohttp fallback (may be blocked by Cloudflare).
    try:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                allow_redirects=True,
                headers={"User-Agent": BROWSER_UA, "Referer": referer},
            ) as response:
                if response.status != 200:
                    logger.warning("Cineby fetch %s -> HTTP %s", url, response.status)
                    return None
                return await response.text(errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        logger.warning("Cineby fetch %s failed: %s", url, exc)
        return None


async def _validate_playlist(url: str) -> str | None:
    """Confirm a candidate serves a real M3U8 with browser identity."""
    def check() -> bool:
        try:
            if curl_requests is not None:
                response = curl_requests.get(
                    url, impersonate="chrome", timeout=20,
                    headers={"Referer": CINEBY_REFERER},
                )
                if response.status_code != 200:
                    return False
                return "#EXTM3U" in response.text[:4096]
            return False
        except Exception:
            return False

    if await asyncio.to_thread(check):
        return url
    return None


async def _try_ytdlp(watch_url: str) -> str | None:
    """Let yt-dlp (browser impersonation) resolve the page directly."""
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "yt_dlp",
            "--no-playlist", "--simulate", "-g",
            "--no-warnings",
            "--extractor-args", "generic:impersonate",
            watch_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.warning("Could not start yt-dlp for Cineby: %s", exc)
        return None

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=120)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return None

    if process.returncode != 0:
        return None
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.lower().endswith(".m3u8") or ".m3u8?" in line:
            candidate = await _validate_playlist(line)
            if candidate:
                logger.info("Cineby playlist resolved via yt-dlp.")
                return candidate
    return None


async def resolve_cineby_playlist(watch_url: str, progress=None) -> str | None:
    """Resolve a playable HLS playlist URL for a Cineby movie watch page."""
    watch_url = watch_url.strip()
    if not is_cineby_url(watch_url):
        raise ValueError("Provide a Cineby watch page (https://cineby.tech/movie/<id>/watch).")

    if progress:
        await progress("Loading the Cineby watch page...")

    page_html = await _fetch_text(watch_url)
    if page_html is None:
        raise RuntimeError(
            "Cineby did not respond (check the bot log for the exact HTTP status). "
            "The page may be down or blocking the bot."
        )
    if "Just a moment" in page_html or "cf-challenge" in page_html:
        raise RuntimeError("Cineby is showing a Cloudflare challenge page.")

    if progress:
        await progress("Scanning the watch page for streams...")
    for candidate in _find_candidates(page_html, watch_url):
        validated = await _validate_playlist(candidate)
        if validated:
            return validated

    embed_urls = [urljoin(watch_url, src) for src in IFRAME_SRC_RE.findall(page_html)][:MAX_EMBEDS]
    for index, embed_url in enumerate(embed_urls, 1):
        if progress:
            await progress(f"Checking player embed {index}/{len(embed_urls)}...")
        embed_html = await _fetch_text(embed_url)
        if embed_html is None:
            continue
        for candidate in _find_candidates(embed_html, embed_url):
            validated = await _validate_playlist(candidate)
            if validated:
                return validated
        for asset_url in [urljoin(embed_url, src) for src in SCRIPT_SRC_RE.findall(embed_html)][:MAX_ASSETS]:
            asset_text = await _fetch_text(asset_url)
            if asset_text is None:
                continue
            for candidate in _find_candidates(asset_text, embed_url):
                validated = await _validate_playlist(candidate)
                if validated:
                    logger.info("Cineby playlist found in embed asset: %s", asset_url)
                    return validated

    for asset_url in [urljoin(watch_url, src) for src in SCRIPT_SRC_RE.findall(page_html)][:MAX_ASSETS]:
        asset_text = await _fetch_text(asset_url)
        if asset_text is None:
            continue
        for candidate in _find_candidates(asset_text, watch_url):
            validated = await _validate_playlist(candidate)
            if validated:
                return validated

    # React players often only fetch streams after JS runs. yt-dlp knows how
    # to drive many such providers without a full browser.
    if progress:
        await progress("Asking yt-dlp to resolve the Cineby page...")
    return await _try_ytdlp(watch_url)


import sys  # noqa: E402  (kept near _try_ytdlp usage for clarity)
