"""vidnest_scraper.py — resolve playable HLS playlists from VidNest movie pages.

Strategies, in order:
  1. Regex the movie page HTML for playlist URLs.
  2. Follow iframe embeds on the page and scan their HTML.
  3. Fetch script/JSON assets referenced by the page and embeds and scan them.
  4. Validate every candidate by requesting it and checking for #EXTM3U.
  5. Optional Playwright network sniff if the above finds nothing.

Note: your bot only downloads what these pages expose; make sure any content
you stream is content you're allowed to stream.
"""

import asyncio
import logging
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import aiohttp

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
PLAYWRIGHT_WAIT_MS = 12_000


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


async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, allow_redirects=True) as response:
            if response.status != 200:
                logger.debug("Asset fetch %s -> HTTP %s", url, response.status)
                return None
            return await response.text(errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        logger.debug("Asset fetch %s failed: %s", url, exc)
        return None


async def _validate_playlist(session: aiohttp.ClientSession, url: str) -> str | None:
    """Confirm a candidate URL actually serves an M3U8 playlist."""
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
            raise RuntimeError("VidNest did not respond. The page may be down or blocking the bot.")

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

            for asset_index, asset_url in enumerate(asset_urls, 1):
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

    # Strategy 4: headless browser network sniff (most reliable, optional).
    if progress:
        await progress("Falling back to a headless browser...")
    playwright_result = await _sniff_with_playwright(movie_url)
    if playwright_result:
        return playwright_result
    return None


async def _sniff_with_playwright(movie_url: str) -> str | None:
    """Load the page in headless Chromium and capture real playlist responses."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.info("Playwright is not installed; skipping the browser fallback.")
        return None

    found: list[str] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"])
            page = await context.new_page()

            def on_response(response) -> None:
                url = response.url
                content_type = response.headers.get("content-type", "")
                if ".m3u8" in url.split("?")[0].rsplit("/", 1)[-1] or "mpegurl" in content_type.lower():
                    found.append(url)

            page.on("response", on_response)
            await page.goto(movie_url, wait_until="domcontentloaded", timeout=45_000)

            # Try to start playback so the player requests its playlist.
            for selector in ("video", "[class*='player']", "iframe"):
                try:
                    element = page.locator(selector).first
                    if await element.count():
                        await element.click(timeout=3_000)
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(PLAYWRIGHT_WAIT_MS)
            await browser.close()
    except Exception as exc:
        logger.warning("Playwright fallback failed: %s", exc)
        return None

    # Prefer the first master playlist the player actually requested.
    return found[0] if found else None
