import asyncio
import json
import logging
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urljoin, urlparse
from html import escape

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
MOVIES_DIR = BASE_DIR / "Movies"
CONFIG_FILE = BASE_DIR / "config.json"
SUPPORTED_EXTENSIONS = {".mp4", ".mp3"}
M3U8_SUFFIX = ".m3u8"
DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
HOST_EXPIRY_BUFFER_SECONDS = 30
# Keep the completed MP4 alive long enough for Discord to fetch and cache its native video preview.
# This is intentionally generous because large M3U8 remuxes and Discord's media crawler
# can take a while before the direct .mp4 URL is fetched.
DISCORD_EMBED_GRACE_SECONDS = 30 * 60
WEB_HOST = os.getenv("MOVIE_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("MOVIE_PORT", "8080"))
HLS_CACHE_DIR = BASE_DIR / ".movie_hls"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
CLOUDFLARED_BIN = os.getenv("CLOUDFLARED_BIN", "cloudflared").strip() or "cloudflared"

active_host = None
host_lock = asyncio.Lock()
cloudflared_process = None
cloudflared_log_task = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("movie-bot")


def ensure_movies_dir() -> None:
    MOVIES_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"channel_id": None}

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {"channel_id": None}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read config.json: %s", exc)
        return {"channel_id": None}


def save_config(config: dict) -> None:
    temp_file = CONFIG_FILE.with_suffix(".json.tmp")
    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)
        file.write("\n")
    temp_file.replace(CONFIG_FILE)


config = load_config()


def get_movie_files() -> list[Path]:
    ensure_movies_dir()
    movies = []
    movies_root = MOVIES_DIR.resolve()

    for path in MOVIES_DIR.iterdir():
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        try:
            resolved = path.resolve()
            if resolved.parent == movies_root:
                movies.append(resolved)
        except OSError:
            continue

    return sorted(movies, key=lambda path: path.stem.casefold())


def find_movie(movie_name: str) -> Path | None:
    requested = movie_name.strip()
    if not requested:
        return None

    if Path(requested).name != requested or "/" in requested or "\\" in requested:
        return None

    requested_stem = Path(requested).stem.casefold()

    for path in get_movie_files():
        if path.stem.casefold() == requested_stem:
            return path

    return None


async def get_movie_channel() -> discord.TextChannel | None:
    channel_id = config.get("channel_id")
    if not isinstance(channel_id, int):
        return None

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    return channel if isinstance(channel, discord.TextChannel) else None


def movie_embed(movies: list[Path], page: int, per_page: int = 10) -> discord.Embed:
    start = page * per_page
    page_movies = movies[start:start + per_page]
    total_pages = max(1, (len(movies) + per_page - 1) // per_page)

    embed = discord.Embed(
        title="Available Movies",
        description="Select a movie below, then press Select This Movie.",
        color=discord.Color.blurple(),
    )

    lines = [
        f"**{start + index + 1}.** {movie.stem}"
        for index, movie in enumerate(page_movies)
    ]
    embed.add_field(name="Movies", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Page {page + 1} of {total_pages} • {len(movies)} total")
    return embed


class MovieSelect(discord.ui.Select):
    def __init__(self, movies: list[Path], owner_id: int, page: int, per_page: int = 10):
        self.owner_id = owner_id
        self.movies = movies
        self.page = page
        self.per_page = per_page

        start = page * per_page
        page_movies = movies[start:start + per_page]

        options = [
            discord.SelectOption(
                label=movie.stem[:100],
                value=movie.name,
                description=f"{movie.suffix.lower().lstrip('.')} media",
            )
            for movie in page_movies
        ]

        super().__init__(
            placeholder="Choose a movie...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This movie list belongs to the member who opened it.",
                ephemeral=True,
            )
            return

        selected_name = self.values[0]
        selected = next(
            (movie for movie in self.movies if movie.name == selected_name),
            None,
        )

        if selected is None or not selected.exists() or not selected.is_file():
            await interaction.response.send_message(
                "That movie is no longer available. Use /movie list to refresh the list.",
                ephemeral=True,
            )
            return

        view = MovieConfirmView(selected, self.owner_id)
        await interaction.response.send_message(
            f"Selected {selected.stem}. Press Select This Movie to send it.",
            view=view,
            ephemeral=True,
        )


class MovieConfirmView(discord.ui.View):
    def __init__(self, movie: Path, owner_id: int):
        super().__init__(timeout=120)
        self.movie = movie
        self.owner_id = owner_id

    @discord.ui.button(label="Select This Movie", style=discord.ButtonStyle.primary)
    async def select_movie(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "You cannot use another member's movie selection.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        async def progress(message: str) -> None:
            try:
                await interaction.edit_original_response(content=message, view=self)
            except discord.HTTPException:
                pass

        await progress("Embedding movie...")
        success, message = await host_movie(self.movie, progress)

        if not success:
            await interaction.edit_original_response(content=message, view=self)
            return

        channel = await get_movie_channel()
        if channel is None:
            await clear_hosted_movie(active_host["token"] if active_host else "")
            await interaction.edit_original_response(content="The configured movie channel is unavailable.", view=self)
            return

        embed = discord.Embed(
            title=f"Now Playing: {self.movie.stem}",
            description=f"[▶ Watch Movie]({message})",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Format", value=self.movie.suffix.lower().lstrip(".").upper())
        embed.set_footer(text="This movie link expires automatically when the movie ends.")

        try:
            await channel.send(content=message, embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            await clear_hosted_movie(active_host["token"] if active_host else "")
            await interaction.edit_original_response(content="I could not post the movie player in the configured channel.", view=self)
            return

        button.disabled = True
        await interaction.edit_original_response(
            content=f"Now hosting {self.movie.stem} in {channel.mention}.",
            view=self,
        )


class MovieListView(discord.ui.View):
    def __init__(self, movies: list[Path], owner_id: int, page: int = 0):
        super().__init__(timeout=180)
        self.movies = movies
        self.owner_id = owner_id
        self.page = page
        self.per_page = 10
        self.total_pages = max(1, (len(movies) + self.per_page - 1) // self.per_page)

        self.add_item(MovieSelect(movies, owner_id, page, self.per_page))

        previous = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            disabled=page == 0,
        )
        previous.callback = self.previous_page
        self.add_item(previous)

        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=page >= self.total_pages - 1,
        )
        next_button.callback = self.next_page
        self.add_item(next_button)

    async def interaction_allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "This movie list belongs to the member who opened it.",
            ephemeral=True,
        )
        return False

    async def previous_page(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_allowed(interaction):
            return
        self.page -= 1
        await self.refresh(interaction)

    async def next_page(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_allowed(interaction):
            return
        self.page += 1
        await self.refresh(interaction)

    async def refresh(self, interaction: discord.Interaction) -> None:
        new_view = MovieListView(self.movies, self.owner_id, self.page)
        await interaction.response.edit_message(
            embed=movie_embed(self.movies, self.page, self.per_page),
            view=new_view,
        )


async def send_movie(movie: Path, requester: discord.abc.User) -> tuple[bool, str]:
    channel = await get_movie_channel()

    if channel is None:
        return (
            False,
            "No valid movie channel is configured. An administrator must run /channel link first.",
        )

    try:
        resolved = movie.resolve()
        movies_root = MOVIES_DIR.resolve()

        if resolved.parent != movies_root or resolved.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return False, "That file is not a supported movie in the Movies folder."

        if not resolved.exists() or not resolved.is_file():
            return False, "That movie is no longer available. Use /movie list to refresh."

        await channel.send(
            content=f"Movie requested by {requester.mention}: {resolved.stem}",
            file=discord.File(resolved, filename=resolved.name),
        )
    except discord.Forbidden:
        return (
            False,
            "I cannot send movies to the configured channel. Check my permissions there.",
        )
    except discord.HTTPException as exc:
        if exc.status == 413 or "too large" in str(exc).lower():
            return (
                False,
                "That movie is too large for Discord's current attachment upload limit.",
            )
        logger.warning("Discord rejected movie upload: %s", exc)
        return False, "Discord rejected the movie upload. Please try again later."
    except OSError as exc:
        logger.warning("Could not read movie file: %s", exc)
        return False, "I could not read that movie file."

    return True, f"Sent {resolved.stem} to <#{channel.id}>."

async def get_media_duration(movie: Path) -> float | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(movie),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return None

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return None

    if process.returncode != 0:
        return None

    try:
        duration = float(stdout.decode("utf-8", errors="replace").strip())
        return duration if duration > 0 else None
    except ValueError:
        return None


async def clear_hosted_movie(token: str, reason: str = "host expired") -> None:
    global active_host

    async with host_lock:
        if active_host is None or active_host["token"] != token:
            return

        current = active_host
        task = current.get("task")
        download_task = current.get("download_task")
        ffmpeg_process = current.get("ffmpeg_process")
        stderr_task = current.get("ffmpeg_stderr_task")
        hls_dir = current.get("hls_dir")
        active_host = None

        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if download_task is not None and download_task is not asyncio.current_task():
            download_task.cancel()
        if stderr_task is not None and stderr_task is not asyncio.current_task():
            stderr_task.cancel()

    if ffmpeg_process is not None and ffmpeg_process.returncode is None:
        ffmpeg_process.terminate()
        try:
            await asyncio.wait_for(ffmpeg_process.wait(), timeout=5)
        except asyncio.TimeoutError:
            ffmpeg_process.kill()
            await ffmpeg_process.wait()

    if hls_dir is not None:
        try:
            for path in hls_dir.rglob("*"):
                if path.is_file():
                    path.unlink(missing_ok=True)
            for path in sorted(hls_dir.rglob("*"), reverse=True):
                if path.is_dir():
                    path.rmdir()
            hls_dir.rmdir()
        except OSError:
            pass

    logger.info("Hosted movie cleanup: %s.", reason)


async def expire_hosted_movie(token: str, duration: float) -> None:
    await asyncio.sleep(duration + HOST_EXPIRY_BUFFER_SECONDS)
    await clear_hosted_movie(token)


async def host_movie(movie: Path, progress=None) -> tuple[bool, str]:
    global active_host

    if not PUBLIC_BASE_URL:
        if progress is not None:
            await progress("Connecting movie player...")
        await bot.ensure_cloudflare_tunnel()

    if not PUBLIC_BASE_URL:
        return False, "Movie streaming is unavailable. Make sure cloudflared is installed and in PATH, then restart the bot."

    try:
        resolved = movie.resolve()
        if resolved.parent != MOVIES_DIR.resolve() or resolved.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return False, "That file is not a supported movie in the Movies folder."
        if not resolved.exists() or not resolved.is_file():
            return False, "That movie is no longer available."
    except OSError:
        return False, "I could not access that movie."

    if progress is not None:
        await progress("Preparing HLS stream...")

    duration = await get_media_duration(resolved)
    if duration is None:
        return False, "I could not determine the movie length. Make sure FFmpeg/ffprobe is installed."

    async with host_lock:
        if active_host is not None:
            remaining = max(0, active_host["expires_at"] - asyncio.get_running_loop().time())
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            return False, f"Another movie is currently being hosted. Please wait about {minutes}m {seconds:02d}s."

        token = secrets.token_urlsafe(32)
        HLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        hls_dir = HLS_CACHE_DIR / token
        hls_dir.mkdir(parents=True, exist_ok=False)
        playlist = hls_dir / "playlist.m3u8"

        try:
            ffmpeg_process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(resolved),
                "-map",
                "0:v:0?",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-start_number",
                "0",
                "-hls_time",
                "6",
                "-hls_list_size",
                "0",
                "-hls_playlist_type",
                "vod",
                "-hls_flags",
                "independent_segments",
                "-f",
                "hls",
                str(playlist),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            try:
                hls_dir.rmdir()
            except OSError:
                pass
            return False, "FFmpeg is not installed or is not in PATH. Install FFmpeg and restart the bot."
        except OSError as exc:
            logger.warning("Could not start HLS FFmpeg process: %s", exc)
            try:
                hls_dir.rmdir()
            except OSError:
                pass
            return False, "I could not start the movie stream."
        except Exception:
            try:
                hls_dir.rmdir()
            except OSError:
                pass
            raise

        active_host = {
            "token": token,
            "movie": resolved,
            "hls_dir": hls_dir,
            "ffmpeg_process": ffmpeg_process,
            "expires_at": asyncio.get_running_loop().time() + duration + HOST_EXPIRY_BUFFER_SECONDS,
            "task": None,
        }
        active_host["task"] = asyncio.create_task(expire_hosted_movie(token, duration))

    # Wait until FFmpeg has produced the playlist so the first viewer does not
    # receive a blank page. Segment files are then requested individually by
    # the HLS player, rather than sending the entire movie at once.
    for _ in range(60):
        if playlist.exists() and playlist.stat().st_size > 0:
            return True, f"{PUBLIC_BASE_URL}/movie/{token}"
        if ffmpeg_process.returncode is not None:
            _, stderr = await ffmpeg_process.communicate()
            error_text = stderr.decode("utf-8", errors="replace").strip()
            logger.warning("HLS preparation failed: %s", error_text[-2000:])
            await clear_hosted_movie(token)
            return False, "FFmpeg could not prepare this movie for streaming."
        await asyncio.sleep(0.5)

    await clear_hosted_movie(token)
    return False, "The movie stream took too long to start."


async def _get_active_host(token: str):
    async with host_lock:
        current = active_host
        if current is None or not secrets.compare_digest(current["token"], token):
            return None

        movie = current["movie"]
        hls_dir = current["hls_dir"]
        try:
            resolved_movie = movie.resolve()
            resolved_hls = hls_dir.resolve()
            if (
                resolved_movie.parent != MOVIES_DIR.resolve()
                or resolved_hls.parent != HLS_CACHE_DIR.resolve()
                or not resolved_hls.is_dir()
            ):
                return None

            # The HLS cache is the source of truth while an M3U8 movie is
            # being prepared. Do not reject the host just because the final
            # MP4 has not been written yet; the browser player can use HLS.
            if not resolved_movie.is_file() and not current.get("downloading"):
                # A completed host must have its final media file available.
                # (The normal local-file host also requires this.)
                return None
        except OSError:
            return None

        return current


async def hosted_movie_handler(request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token", "")
    current = await _get_active_host(token)
    if current is None:
        return web.Response(status=404, text="Movie is no longer being hosted.")

    movie = current["movie"]
    title = escape(movie.stem)
    playlist_url = f"/hls/{token}/playlist.m3u8"
    parts = current.get("parts")
    part_urls = (
        [f"/parts/{token}/part1.mp4", f"/parts/{token}/part2.mp4"]
        if parts is not None and len(parts) == 2
        else []
    )

    if movie.suffix.lower() == ".mp4":
        player = f"""
        <video id="player" controls playsinline preload="metadata"
               style="width:100%;max-height:78vh;background:#000;border-radius:12px"></video>
        <script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
        <script>
        const video = document.getElementById("player");
        const parts = {json.dumps(part_urls)};
        const hlsSource = {json.dumps(playlist_url)};

        async function playHostedMovie() {{
            // Once both large parts exist, append them to one MediaSource.
            // This avoids changing the video element's src at the halfway point.
            if (parts.length === 2 && window.MediaSource && MediaSource.isTypeSupported("video/mp4")) {{
                try {{
                    const mediaSource = new MediaSource();
                    video.src = URL.createObjectURL(mediaSource);
                    await new Promise(resolve =>
                        mediaSource.addEventListener("sourceopen", resolve, {{once: true}})
                    );
                    const buffer = mediaSource.addSourceBuffer("video/mp4");

                    for (const url of parts) {{
                        const response = await fetch(url);
                        if (!response.ok) throw new Error("Movie part unavailable");
                        const data = await response.arrayBuffer();
                        await new Promise((resolve, reject) => {{
                            const onEnd = () => {{
                                buffer.removeEventListener("updateend", onEnd);
                                resolve();
                            }};
                            const onError = () => {{
                                buffer.removeEventListener("updateend", onEnd);
                                reject(new Error("Media buffer error"));
                            }};
                            buffer.addEventListener("updateend", onEnd);
                            buffer.addEventListener("error", onError, {{once: true}});
                            buffer.appendBuffer(data);
                        }});
                    }}

                    if (mediaSource.readyState === "open") mediaSource.endOfStream();
                    document.getElementById("status").textContent = "Playing movie";
                    return;
                }} catch (error) {{
                    console.warn("Two-part playback unavailable, using HLS fallback.", error);
                }}
            }}

            if (video.canPlayType("application/vnd.apple.mpegurl")) {{
                video.src = hlsSource;
            }} else if (window.Hls && Hls.isSupported()) {{
                const hls = new Hls({{
                    enableWorker: true,
                    lowLatencyMode: false,
                    backBufferLength: 30
                }});
                hls.loadSource(hlsSource);
                hls.attachMedia(video);
            }} else {{
                document.getElementById("status").textContent =
                    "This browser does not support HLS playback.";
            }}
        }}

        playHostedMovie();
        </script>
        """
    else:
        player = f"""
        <audio id="player" controls preload="metadata" style="width:100%"></audio>
        <script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
        <script>
        const audio = document.getElementById("player");
        const source = {json.dumps(playlist_url)};
        if (audio.canPlayType("application/vnd.apple.mpegurl")) {{
            audio.src = source;
        }} else if (window.Hls && Hls.isSupported()) {{
            const hls = new Hls();
            hls.loadSource(source);
            hls.attachMedia(audio);
        }} else {{
            document.getElementById("status").textContent =
                "This browser does not support HLS playback.";
        }}
        </script>
        """

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - Movie Bot</title>
<style>
body {{
    margin: 0;
    min-height: 100vh;
    background: #101114;
    color: #fff;
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
main {{
    width: min(1100px, calc(100% - 24px));
    margin: 0 auto;
    padding: 16px 0 24px;
}}
h1 {{
    margin: 0 0 12px;
    font-size: clamp(20px, 3vw, 30px);
}}
.player {{
    background: #000;
    border-radius: 14px;
    overflow: hidden;
}}
.actions {{
    display: flex;
    gap: 8px;
    margin-top: 10px;
}}
.button {{
    display: inline-block;
    padding: 9px 14px;
    border-radius: 6px;
    background: #5865f2;
    color: #fff;
    text-decoration: none;
    border: 0;
    font: inherit;
    cursor: pointer;
}}
.button:disabled {{
    opacity: .5;
    cursor: not-allowed;
}}
#status {{
    margin-top: 9px;
    color: #aaa;
    font-size: 14px;
}}
</style>
</head>
<body>
<main>
<h1>{title}</h1>
<div class="player">{player}</div>
<div class="actions">
    <button id="download" class="button" disabled>Download Movie</button>
</div>
<div id="status">Loading movie stream...</div>
</main>
<script>
const downloadButton = document.getElementById("download");
const downloadUrl = "/media/{token}?download=1";

async function updateDownloadButton() {{
    try {{
        const response = await fetch("/status/{token}", {{cache: "no-store"}});
        if (!response.ok) throw new Error("status unavailable");
        const data = await response.json();

        if (data.ready) {{
            downloadButton.disabled = false;
            downloadButton.onclick = () => {{
                window.location.href = downloadUrl;
            }};
            return;
        }}

        downloadButton.disabled = true;
    }} catch (error) {{
        downloadButton.disabled = true;
    }}
}}

updateDownloadButton();
setInterval(updateDownloadButton, 5000);
</script>
</body>
</html>"""
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def hosted_media_handler(request: web.Request) -> web.StreamResponse:
    """Serve a completed MP4 with HTTP range support for Discord/media players."""
    token = request.match_info.get("token", "")
    current = await _get_active_host(token)
    if current is None:
        return web.Response(status=404, text="Movie is no longer being hosted.")

    movie = current["movie"]
    if movie.suffix.lower() != ".mp4":
        return web.Response(status=404, text="Direct video preview is unavailable for this media.")

    try:
        resolved = movie.resolve()
        if resolved.parent != MOVIES_DIR.resolve():
            return web.Response(status=404, text="Movie is unavailable.")
        if not resolved.is_file():
            # An active M3U8 host may not have its final MP4 yet. Always send
            # it to the HLS player instead of exposing a misleading readiness
            # error.
            raise web.HTTPFound(location=f"/movie/{token}")
    except web.HTTPException:
        raise
    except OSError:
        raise web.HTTPFound(location=f"/movie/{token}")

    # aiohttp's FileResponse handles byte ranges, which Discord and browsers
    # need when probing/streaming large MP4 files.
    wants_download = request.query.get("download") == "1"
    return web.FileResponse(
        path=resolved,
        headers={
            "Content-Type": "video/mp4",
            "Content-Disposition": (
                f'attachment; filename="{escape(movie.name)}"'
                if wants_download
                else f'inline; filename="{escape(movie.name)}"'
            ),
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Accept-Ranges": "bytes",
        },
    )

async def hosted_status_handler(request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token", "")
    async with host_lock:
        current = active_host
        if current is None or not secrets.compare_digest(current["token"], token):
            return web.json_response({"ready": False}, status=404)

        movie = current["movie"]
        ready = (
            not current.get("downloading", False)
            and movie.suffix.lower() == ".mp4"
            and movie.is_file()
        )

    return web.json_response({"ready": ready})

async def hosted_part_handler(request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token", "")
    filename = request.match_info.get("filename", "")
    current = await _get_active_host(token)
    if current is None:
        return web.Response(status=404, text="Movie is no longer being hosted.")

    if filename not in {"part1.mp4", "part2.mp4"}:
        return web.Response(status=404, text="Movie part not found.")

    parts_dir = current.get("parts_dir")
    if parts_dir is None:
        return web.Response(status=404, text="Movie parts are not ready.")

    target = Path(parts_dir) / filename
    try:
        resolved = target.resolve()
        if resolved.parent != Path(parts_dir).resolve() or not resolved.is_file():
            return web.Response(status=404, text="Movie part not found.")
    except OSError:
        return web.Response(status=404, text="Movie part not found.")

    return web.FileResponse(
        path=resolved,
        headers={
            "Content-Type": "video/mp4",
            "Content-Disposition": "inline",
            "Cache-Control": "no-store",
            "Accept-Ranges": "bytes",
        },
    )


async def hosted_hls_handler(request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token", "")
    filename = request.match_info.get("filename", "")

    current = await _get_active_host(token)
    if current is None:
        return web.Response(status=404, text="Movie is no longer being hosted.")

    if Path(filename).name != filename or filename in {".", ".."}:
        return web.Response(status=400, text="Invalid stream resource.")

    lower_filename = filename.lower()
    if not (
        filename == "playlist.m3u8"
        or lower_filename.endswith(".ts")
        or lower_filename.endswith(".m4s")
        or lower_filename == "init.mp4"
    ):
        return web.Response(status=404, text="Stream resource not found.")

    target = current["hls_dir"] / filename
    try:
        resolved_target = target.resolve()
        if resolved_target.parent != current["hls_dir"].resolve() or not resolved_target.is_file():
            return web.Response(status=404, text="Stream resource not found.")
    except OSError:
        return web.Response(status=404, text="Stream resource not found.")

    if filename == "playlist.m3u8":
        return web.FileResponse(
            path=resolved_target,
            headers={
                "Content-Type": "application/vnd.apple.mpegurl",
                "Cache-Control": "no-store",
            },
        )

    return web.FileResponse(
        path=resolved_target,
        headers={
            "Content-Type": (
                "video/iso.segment"
                if lower_filename.endswith(".m4s")
                else "video/mp4"
                if lower_filename == "init.mp4"
                else "video/mp2t"
            ),
            "Cache-Control": "no-store",
            "Accept-Ranges": "bytes",
        },
    )


cloudflare_start_lock = asyncio.Lock()

async def start_cloudflare_quick_tunnel() -> bool:
    global PUBLIC_BASE_URL, cloudflared_process, cloudflared_log_task

    if PUBLIC_BASE_URL:
        return True

    async with cloudflare_start_lock:
        if PUBLIC_BASE_URL:
            return True

        # If another caller already started cloudflared, wait for that caller
        # to finish instead of incorrectly reporting that the tunnel failed.
        if cloudflared_process is not None:
            for _ in range(300):
                if PUBLIC_BASE_URL:
                    return True
                if cloudflared_process.returncode is not None:
                    cloudflared_process = None
                    break
                await asyncio.sleep(0.5)
            return bool(PUBLIC_BASE_URL)

        try:
            cloudflared_process = await asyncio.create_subprocess_exec(
                CLOUDFLARED_BIN,
                "tunnel",
                "--url",
                f"http://127.0.0.1:{WEB_PORT}",
                "--no-autoupdate",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            logger.error(
                "cloudflared was not found. Install cloudflared and make sure it is in PATH."
            )
            cloudflared_process = None
            return False
        except OSError as exc:
            logger.error("Could not start cloudflared: %s", exc)
            cloudflared_process = None
            return False

        # Quick Tunnel output is human-readable and may contain prefixes,
        # timestamps, whitespace, or other log text. Match the URL anywhere
        # in the line rather than assuming a specific hostname shape.
        url_pattern = re.compile(
            r"""https://[^\s"'<>]+\.trycloudflare\.com(?:/[^\s"'<>]*)?""",
            re.IGNORECASE,
        )

        try:
            while True:
                if cloudflared_process.stdout is None:
                    break

                line = await asyncio.wait_for(
                    cloudflared_process.stdout.readline(),
                    timeout=90,
                )
                if not line:
                    break

                text_line = line.decode("utf-8", errors="replace").strip()
                match = url_pattern.search(text_line)
                if match:
                    PUBLIC_BASE_URL = match.group(0).rstrip("/.,;")
                    logger.info("Cloudflare Quick Tunnel URL: %s", PUBLIC_BASE_URL)
                    cloudflared_log_task = asyncio.create_task(
                        _drain_cloudflared_output(cloudflared_process)
                    )
                    return True

        except asyncio.TimeoutError:
            logger.error("Timed out waiting for cloudflared to provide a public URL.")
        except (OSError, RuntimeError) as exc:
            logger.error("Could not read cloudflared output: %s", exc)

        if cloudflared_process.returncode is None:
            cloudflared_process.terminate()
            try:
                await asyncio.wait_for(cloudflared_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                cloudflared_process.kill()
                await cloudflared_process.wait()

        cloudflared_process = None
        return False


async def _drain_cloudflared_output(process: asyncio.subprocess.Process) -> None:
    if process.stdout is None:
        return

    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text_line = line.decode("utf-8", errors="replace").strip()
            if text_line:
                logger.debug("cloudflared: %s", text_line)
    except asyncio.CancelledError:
        raise
    except OSError as exc:
        logger.debug("cloudflared output reader stopped: %s", exc)


async def stop_cloudflare_quick_tunnel() -> None:
    global cloudflared_process, cloudflared_log_task, PUBLIC_BASE_URL

    if cloudflared_log_task is not None:
        cloudflared_log_task.cancel()
        try:
            await cloudflared_log_task
        except asyncio.CancelledError:
            pass
        cloudflared_log_task = None

    process = cloudflared_process
    cloudflared_process = None
    if process is not None and process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    PUBLIC_BASE_URL = ""


async def hosted_cdn_handler(request: web.Request) -> web.StreamResponse:
    """Serve completed MP4s from a direct .mp4 URL for Discord media detection."""
    token = request.match_info["token"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", token):
        raise web.HTTPNotFound()

    async with host_lock:
        current = active_host
        if current is None or not secrets.compare_digest(current["token"], token):
            raise web.HTTPNotFound()
        movie = current["movie"]
        downloading = current.get("downloading", False)

    if downloading or not movie.exists() or not movie.is_file():
        raise web.HTTPNotFound()

    try:
        resolved = movie.resolve()
        if resolved.parent != MOVIES_DIR.resolve() or resolved.suffix.lower() != ".mp4":
            raise web.HTTPNotFound()
    except OSError:
        raise web.HTTPNotFound()

    return web.FileResponse(
        resolved,
        headers={
            "Content-Type": "video/mp4",
            "Content-Disposition": "inline",
            "Cache-Control": "public, max-age=30",
            "Accept-Ranges": "bytes",
            "X-Content-Type-Options": "nosniff",
        },
    )

async def start_movie_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/movie/{token}", hosted_movie_handler)
    app.router.add_get("/status/{token}", hosted_status_handler)
    app.router.add_get("/media/{token}", hosted_media_handler)
    app.router.add_get("/cdn/{token}.mp4", hosted_cdn_handler)
    app.router.add_get("/parts/{token}/{filename}", hosted_part_handler)
    app.router.add_get("/hls/{token}/{filename}", hosted_hls_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
    logger.info("Movie web server listening on %s:%d", WEB_HOST, WEB_PORT)
    return runner


class ChannelLinkView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Choose the movie channel...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        self.channel_select.callback = self.channel_selected
        self.add_item(self.channel_select)

    async def channel_selected(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This channel selector belongs to the administrator who opened it.",
                ephemeral=True,
            )
            return

        selected_channel = self.channel_select.values[0]

        # Resolve the selected channel by ID so Discord's ChannelSelect
        # response works consistently across channel object types.
        channel_id = getattr(selected_channel, "id", None)
        if not isinstance(channel_id, int):
            await interaction.response.send_message(
                "I could not read the selected channel. Please try again.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(channel_id) if interaction.guild else None
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Please select a text channel.",
                ephemeral=True,
            )
            return

        config["channel_id"] = channel.id
        try:
            save_config(config)
        except OSError as exc:
            logger.error("Could not save config: %s", exc)
            await interaction.response.send_message(
                "I could not save the channel configuration.",
                ephemeral=True,
            )
            return

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"Movie channel linked to {channel.mention}.",
            view=self,
        )
        self.stop()


class MovieBot(discord.Client):
    def __init__(self) -> None:
        self.movie_web_runner: web.AppRunner | None = None
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        self.movie_web_runner = await start_movie_web_server()

        # Do not block Discord login while cloudflared starts.
        asyncio.create_task(self.ensure_cloudflare_tunnel())

        try:
            synced = await self.tree.sync()
            logger.info("Synced %d slash command(s).", len(synced))
        except discord.HTTPException as exc:
            logger.error("Failed to sync slash commands: %s", exc)

    async def ensure_cloudflare_tunnel(self) -> bool:
        if PUBLIC_BASE_URL:
            return True

        tunnel_started = await start_cloudflare_quick_tunnel()
        if not tunnel_started:
            logger.warning(
                "Movie web hosting is unavailable until cloudflared is installed and running."
            )
        return tunnel_started

    async def close(self) -> None:
        await stop_cloudflare_quick_tunnel()
        if self.movie_web_runner is not None:
            await self.movie_web_runner.cleanup()
            self.movie_web_runner = None
        await super().close()

    async def on_ready(self) -> None:
        if self.user:
            logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)


bot = MovieBot()

channel_group = app_commands.Group(
    name="channel",
    description="Configure the movie channel.",
)
movie_group = app_commands.Group(
    name="movie",
    description="Browse and send available movies.",
)


@channel_group.command(name="link", description="Choose the channel where movies will be sent.")
@app_commands.checks.has_permissions(administrator=True)
async def channel_link(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "Choose the Discord text channel where movies should be sent:",
        view=ChannelLinkView(interaction.user.id),
        ephemeral=True,
    )


@movie_group.command(name="list", description="Privately list all available movies.")
async def movie_list(interaction: discord.Interaction) -> None:
    movies = get_movie_files()

    if not movies:
        await interaction.response.send_message(
            "No movies are currently available.",
            ephemeral=True,
        )
        return

    view = MovieListView(movies, interaction.user.id)
    await interaction.response.send_message(
        embed=movie_embed(movies, 0, view.per_page),
        view=view,
        ephemeral=True,
    )


def is_m3u8_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    path = parsed.path.lower()
    return path.endswith(".m3u8") or ".m3u8" in path


def safe_download_name(url: str) -> str:
    parsed = urlparse(url)
    raw_name = Path(parsed.path).stem or "movie"
    raw_name = re.sub(r"[^A-Za-z0-9._ -]+", "", raw_name).strip(" .")
    raw_name = raw_name[:80] or "movie"

    candidate = MOVIES_DIR / f"{raw_name}.mp4"
    number = 2
    while candidate.exists():
        candidate = MOVIES_DIR / f"{raw_name} ({number}).mp4"
        number += 1
    return candidate.name


async def fetch_m3u8_text(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
    async with session.get(url, allow_redirects=True) as response:
        response.raise_for_status()
        text = await response.text(errors="replace")
        return text, str(response.url)


def parse_m3u8_segments(playlist_text: str, base_url: str) -> tuple[list[tuple[float, str]], str | None, str | None]:
    """Parse a simple VOD media playlist.

    Returns (segments, init_url, unsupported_reason). Encrypted and byte-range
    playlists fall back to FFmpeg because they need special handling.
    """
    if "#EXT-X-KEY:" in playlist_text:
        for line in playlist_text.splitlines():
            if line.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in line.upper():
                return [], None, "encrypted HLS"
    if "#EXT-X-BYTERANGE:" in playlist_text:
        return [], None, "byte-range HLS"

    segments: list[tuple[float, str]] = []
    init_url = None
    pending_duration = None

    lines = [line.strip() for line in playlist_text.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                init_url = urljoin(base_url, match.group(1))
        elif line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                pending_duration = 2.0
        elif not line.startswith("#") and pending_duration is not None:
            segments.append((pending_duration, urljoin(base_url, line)))
            pending_duration = None

    if not segments:
        return [], init_url, "no media segments found"

    return segments, init_url, None


async def resolve_m3u8_media_playlist(
    session: aiohttp.ClientSession,
    url: str,
) -> tuple[str, list[tuple[float, str]], str | None, str | None]:
    """Resolve a master playlist to the highest-bandwidth media playlist."""
    text, final_url = await fetch_m3u8_text(session, url)

    if "#EXT-X-STREAM-INF:" not in text:
        segments, init_url, reason = parse_m3u8_segments(text, final_url)
        return final_url, segments, init_url, reason

    variants: list[tuple[int, str]] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        next_line = lines[index + 1]
        if next_line.startswith("#"):
            continue
        bandwidth_match = re.search(r"(?:AVERAGE-BANDWIDTH|BANDWIDTH)=(\d+)", line)
        bandwidth = int(bandwidth_match.group(1)) if bandwidth_match else 0
        variants.append((bandwidth, urljoin(final_url, next_line)))

    if not variants:
        return final_url, [], None, "master playlist has no variants"

    variants.sort(key=lambda item: item[0], reverse=True)
    media_url = variants[0][1]
    media_text, media_final_url = await fetch_m3u8_text(session, media_url)
    segments, init_url, reason = parse_m3u8_segments(media_text, media_final_url)
    return media_final_url, segments, init_url, reason


async def download_m3u8_segments(
    url: str,
    hls_dir: Path,
    progress=None,
) -> tuple[bool, str]:
    """Download HLS media segments concurrently and build a local playlist.

    This is substantially faster than FFmpeg's sequential HLS fetching for
    sources that allow several HTTP requests at once. If the source uses
    encryption or byte ranges, the caller falls back to FFmpeg.
    """
    timeout = aiohttp.ClientTimeout(
        total=DOWNLOAD_TIMEOUT_SECONDS,
        connect=30,
        sock_read=120,
    )
    # Some CDNs (including signed media CDNs) reject large bursts of requests.
    # Eight workers is still much faster than sequential fetching while being
    # considerably more compatible with protected HLS sources.
    connector = aiohttp.TCPConnector(limit=16, limit_per_host=8, ttl_dns_cache=300)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        headers=headers,
    ) as session:
        try:
            media_url, segments, init_url, reason = await resolve_m3u8_media_playlist(session, url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return False, f"playlist fetch failed: {exc}"

        # Signed CDNs often expect the segment requests to originate from the
        # playlist URL. Supplying the playlist as Referer improves compatibility
        # without exposing or changing the signed segment URLs.
        playlist_origin = f"{urlparse(media_url).scheme}://{urlparse(media_url).netloc}"
        session.headers.update({
            "Referer": media_url,
            "Origin": playlist_origin,
        })

        if reason:
            return False, reason

        hls_dir.mkdir(parents=True, exist_ok=True)
        segment_extension = ".m4s" if init_url else ".ts"
        segment_names = [
            f"segment-{index:06d}{segment_extension}"
            for index in range(len(segments))
        ]

        if init_url:
            init_path = hls_dir / "init.mp4"
            try:
                async with session.get(init_url) as response:
                    response.raise_for_status()
                    with init_path.open("wb") as file:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            file.write(chunk)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                return False, f"initialization segment download failed: {exc}"

        completed = [False] * len(segments)
        lock = asyncio.Lock()
        completed_bytes = 0
        started_at = asyncio.get_running_loop().time()
        forbidden_count = 0
        fallback_requested = asyncio.Event()

        async def write_playlist(final: bool = False) -> None:
            count = 0
            while count < len(completed) and completed[count]:
                count += 1

            target_duration = max(1, int(max(duration for duration, _ in segments) + 0.999))
            lines = [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                f"#EXT-X-TARGETDURATION:{target_duration}",
                "#EXT-X-MEDIA-SEQUENCE:0",
                "#EXT-X-PLAYLIST-TYPE:EVENT",
            ]
            if init_url:
                lines.append('#EXT-X-MAP:URI="init.mp4"')

            for index in range(count):
                duration, _ = segments[index]
                lines.append(f"#EXTINF:{duration:.6f},")
                lines.append(segment_names[index])

            if final and count == len(segments):
                lines.append("#EXT-X-ENDLIST")

            temp = hls_dir / "playlist.m3u8.tmp"
            temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            temp.replace(hls_dir / "playlist.m3u8")

        async def worker(index: int) -> None:
            nonlocal completed_bytes, forbidden_count
            segment_url = segments[index][1]
            target = hls_dir / segment_names[index]

            for attempt in range(4):
                try:
                    async with session.get(segment_url) as response:
                        response.raise_for_status()
                        with target.open("wb") as file:
                            async for chunk in response.content.iter_chunked(1024 * 1024):
                                file.write(chunk)
                                completed_bytes += len(chunk)

                    async with lock:
                        completed[index] = True
                        await write_playlist()

                    elapsed = max(0.1, asyncio.get_running_loop().time() - started_at)
                    speed_mbps = (completed_bytes * 8 / elapsed) / 1_000_000
                    done = sum(completed)
                    if progress is not None and (done == 1 or done % 4 == 0 or done == len(segments)):
                        await progress(
                            f"Downloading movie... {done}/{len(segments)} segments "
                            f"({speed_mbps:.1f} Mbps)"
                        )
                    return
                except (aiohttp.ClientResponseError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    target.unlink(missing_ok=True)
                    if isinstance(exc, aiohttp.ClientResponseError) and exc.status == 403:
                        async with lock:
                            forbidden_count += 1
                            current_forbidden = forbidden_count
                        logger.warning(
                            "Segment %d was rejected with HTTP 403 (%d blocked requests).",
                            index + 1,
                            current_forbidden,
                        )
                        if current_forbidden >= 3:
                            fallback_requested.set()
                            raise RuntimeError("Too many HTTP 403 responses; switching to FFmpeg.")
                        if attempt < 1:
                            await asyncio.sleep(0.5)
                            continue
                        raise RuntimeError(f"segment {index + 1} failed with HTTP 403") from exc
                    if attempt == 3:
                        raise RuntimeError(f"segment {index + 1} failed: {exc}") from exc
                    await asyncio.sleep(1.5 * (attempt + 1))

        try:
            await asyncio.gather(*(worker(index) for index in range(len(segments))))
            await write_playlist(final=True)
        except Exception as exc:
            if fallback_requested.is_set():
                logger.warning(
                    "M3U8 CDN returned repeated HTTP 403 responses; switching to FFmpeg fallback."
                )
                (hls_dir / "playlist.m3u8").unlink(missing_ok=True)
                (hls_dir / "playlist.m3u8.tmp").unlink(missing_ok=True)
                for partial in hls_dir.glob("segment-*"):
                    partial.unlink(missing_ok=True)
            else:
                logger.warning(
                    "Parallel M3U8 download failed: %s; falling back to FFmpeg.",
                    exc,
                )
            return await download_m3u8_with_ffmpeg(
                url,
                hls_dir,
                media_url,
                progress,
            )

    logger.info(
        "Parallel M3U8 download completed: %d segments.",
        len(segments),
    )
    return True, media_url


async def download_m3u8_with_ffmpeg(
    url: str,
    hls_dir: Path,
    media_url: str | None = None,
    progress=None,
) -> tuple[bool, str]:
    """Fallback HLS downloader for CDNs that reject individual aiohttp requests."""
    hls_dir.mkdir(parents=True, exist_ok=True)
    playlist = hls_dir / "playlist.m3u8"
    playlist.unlink(missing_ok=True)
    for partial in hls_dir.glob("segment-*"):
        partial.unlink(missing_ok=True)
    origin_url = media_url or url
    parsed = urlparse(origin_url)
    headers = (
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36\\r\\n"
        f"Referer: {origin_url}\\r\\n"
        f"Origin: {parsed.scheme}://{parsed.netloc}\\r\\n"
    )

    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-headers", headers,
        "-i", origin_url,
        "-c", "copy",
        "-f", "hls",
        "-hls_time", "6",
        "-hls_playlist_type", "event",
        "-hls_segment_filename", str(hls_dir / "segment-%06d.ts"),
        str(playlist),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    if progress is not None:
        await progress("Downloading movie... switching to FFmpeg CDN fallback.")

    try:
        _, stderr = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise

    if process.returncode != 0 or not playlist.is_file():
        error = stderr.decode("utf-8", errors="replace").strip()
        return False, error[-500:] or "FFmpeg HLS download failed."

    return True, origin_url


async def stream_m3u8_movie(url: str, progress=None) -> tuple[bool, str, Path | None]:
    """Download M3U8 segments in parallel while exposing a growing HLS playlist."""
    global active_host

    ensure_movies_dir()

    if not is_m3u8_url(url):
        return False, "That is not a valid HTTP/HTTPS M3U8 URL.", None

    if progress is not None:
        await progress("Downloading movie...")

    if not PUBLIC_BASE_URL:
        if progress is not None:
            await progress("Connecting movie player...")
        await bot.ensure_cloudflare_tunnel()

    if not PUBLIC_BASE_URL:
        return False, "Movie streaming is unavailable. Make sure cloudflared is installed and in PATH, then restart the bot."

    filename = safe_download_name(url)
    output = MOVIES_DIR / filename

    async with host_lock:
        if active_host is not None:
            return False, "Another movie is currently being hosted. Please wait until it finishes.", None

        token = secrets.token_urlsafe(32)
        HLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        hls_dir = HLS_CACHE_DIR / token
        hls_dir.mkdir(parents=True, exist_ok=False)
        playlist = hls_dir / "playlist.m3u8"

        active_host = {
            "token": token,
            "movie": output,
            "hls_dir": hls_dir,
            "ffmpeg_process": None,
            "ffmpeg_stderr_lines": [],
            "ffmpeg_stderr_task": None,
            "expires_at": float("inf"),
            "task": None,
            "download_task": None,
            "downloading": True,
        }

        download_task = asyncio.create_task(
            download_m3u8_segments(url, hls_dir, progress)
        )
        active_host["download_task"] = download_task
        active_host["task"] = asyncio.create_task(
            finish_m3u8_host(token, download_task, playlist, output)
        )

    # Wait for the first contiguous segment. Parallel workers can be downloading
    # later segments already, so playback can begin while the rest downloads.
    for _ in range(300):
        if playlist.exists() and playlist.stat().st_size > 0:
            try:
                playlist_text = playlist.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                playlist_text = ""

            has_segment = any(hls_dir.glob("*.ts")) or any(hls_dir.glob("*.m4s"))
            if "#EXTINF:" in playlist_text and has_segment:
                if progress is not None:
                    await progress("Embedding movie...")
                logger.info("M3U8 playback ready: %s", token)
                return True, f"{PUBLIC_BASE_URL}/movie/{token}", output

        if download_task.done():
            try:
                success, detail = download_task.result()
            except Exception as exc:
                success, detail = False, str(exc)

            if not success:
                logger.warning("Parallel M3U8 downloader failed: %s", detail)
                await clear_hosted_movie(token, reason="parallel M3U8 downloader failed")
                return False, "The M3U8 downloader could not start. Check the bot console for details.", None

        if _ % 10 == 0:
            logger.info("Waiting for parallel M3U8 playback data... %.1fs", _ * 0.5)

        await asyncio.sleep(0.5)

    await clear_hosted_movie(token, reason="M3U8 startup timeout")
    return False, "The M3U8 stream took too long to start. Check the bot console for the downloader error.", None


async def _collect_ffmpeg_stderr(process: asyncio.subprocess.Process, lines: list[str]) -> None:
    if process.stderr is None:
        return
    try:
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            value = line.decode("utf-8", errors="replace").rstrip()
            if value:
                lines.append(value)
                if len(lines) > 100:
                    del lines[:-100]
    except asyncio.CancelledError:
        raise


async def get_movie_metadata_title(movie: Path) -> str | None:
    """Read a useful title from media metadata, if the source provides one."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format_tags=title",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(movie),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except (FileNotFoundError, OSError, asyncio.TimeoutError):
        return None

    if process.returncode != 0:
        return None

    title = stdout.decode("utf-8", errors="replace").strip()
    title = re.sub(r"\\s+", " ", title).strip(" .")
    if not title:
        return None

    # Avoid replacing a useful filename with a generic player/stream name.
    generic = {"index", "movie", "video", "stream", "playlist"}
    if title.casefold() in generic:
        return None
    return title[:120]


async def rename_movie_from_metadata(movie: Path) -> Path:
    title = await get_movie_metadata_title(movie)
    if not title:
        return movie

    safe_title = re.sub(r"[^A-Za-z0-9._ -]+", "", title).strip(" .")
    safe_title = safe_title[:100].strip(" .")
    if not safe_title or safe_title.casefold() == movie.stem.casefold():
        return movie

    candidate = MOVIES_DIR / f"{safe_title}{movie.suffix.lower()}"
    number = 2
    while candidate.exists() and candidate.resolve() != movie.resolve():
        candidate = MOVIES_DIR / f"{safe_title} ({number}){movie.suffix.lower()}"
        number += 1

    try:
        movie.replace(candidate)
        logger.info("Using media metadata title for movie name: %s", candidate.stem)
        return candidate
    except OSError as exc:
        logger.warning("Could not rename movie from metadata: %s", exc)
        return movie


async def split_movie_into_parts(source: Path, parts_dir: Path) -> tuple[Path, Path] | None:
    """Create two fragmented MP4 files for site-side sequential playback."""
    parts_dir.mkdir(parents=True, exist_ok=True)

    try:
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await probe.communicate()
        if probe.returncode != 0:
            logger.warning("Could not determine movie duration: %s",
                           stderr.decode("utf-8", errors="replace")[-2000:])
            return None
        duration = float(stdout.decode().strip())
    except (FileNotFoundError, OSError, ValueError):
        return None

    if duration <= 2:
        return None

    halfway = duration / 2
    first = parts_dir / "part1.mp4"
    second = parts_dir / "part2.mp4"
    common = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c", "copy",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
    ]

    try:
        p1 = await asyncio.create_subprocess_exec(
            *common, "-t", str(halfway), str(first),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, e1 = await p1.communicate()
        if p1.returncode != 0 or not first.exists() or first.stat().st_size == 0:
            logger.warning("Movie part 1 creation failed: %s",
                           e1.decode("utf-8", errors="replace")[-3000:])
            first.unlink(missing_ok=True)
            return None

        p2 = await asyncio.create_subprocess_exec(
            *common, "-ss", str(halfway), str(second),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, e2 = await p2.communicate()
        if p2.returncode != 0 or not second.exists() or second.stat().st_size == 0:
            logger.warning("Movie part 2 creation failed: %s",
                           e2.decode("utf-8", errors="replace")[-3000:])
            first.unlink(missing_ok=True)
            second.unlink(missing_ok=True)
            return None

        logger.info("Created two site-hosted movie parts.")
        return first, second
    except (FileNotFoundError, OSError) as exc:
        logger.warning("Movie part creation failed: %s", exc)
        first.unlink(missing_ok=True)
        second.unlink(missing_ok=True)
        return None


async def finish_m3u8_host(
    token: str,
    download_task: asyncio.Task,
    playlist: Path,
    output: Path,
) -> None:
    try:
        try:
            success, detail = await download_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            success, detail = False, str(exc)

        if not success:
            logger.error("Parallel M3U8 download failed: %s", detail)
            await clear_hosted_movie(token, reason="M3U8 download failed")
            return

        if not playlist.exists() or playlist.stat().st_size == 0:
            await clear_hosted_movie(token, reason="M3U8 playlist missing after download")
            return

        # Convert the locally cached segments into one MP4. No second network
        # download is performed here.
        if output.exists():
            output.unlink(missing_ok=True)

        remux_temp = output.with_name(f".{output.name}.part")
        try:
            remux = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner", "-loglevel", "error", "-y",
                "-allowed_extensions", "ALL",
                "-i", str(playlist),
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                "-movflags", "+faststart",
                str(remux_temp),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, remux_stderr = await remux.communicate()
            if remux.returncode != 0 or not remux_temp.exists() or remux_temp.stat().st_size == 0:
                logger.warning(
                    "Could not save completed M3U8 movie as MP4: %s",
                    remux_stderr.decode("utf-8", errors="replace")[-5000:],
                )
                remux_temp.unlink(missing_ok=True)
                await clear_hosted_movie(token, reason="M3U8 MP4 remux failed")
                return
            remux_temp.replace(output)

            # Some M3U8 URLs use generic names such as index-f2-v1-a1.
            # Prefer the actual title embedded in the media when available.
            renamed_output = await rename_movie_from_metadata(output)
            if renamed_output != output:
                output = renamed_output
                async with host_lock:
                    current = active_host
                    if current is not None and current["token"] == token:
                        current["movie"] = output
        except (FileNotFoundError, OSError) as exc:
            logger.warning("Could not remux completed M3U8 movie: %s", exc)
            remux_temp.unlink(missing_ok=True)
            await clear_hosted_movie(token, reason="M3U8 MP4 remux failed")
            return

        parts_dir = HLS_CACHE_DIR / token / "parts"
        parts = await split_movie_into_parts(output, parts_dir) if output.exists() else None

        async with host_lock:
            current = active_host
            if current is not None and current["token"] == token and parts is not None:
                current["parts_dir"] = parts_dir
                current["parts"] = parts

        async with host_lock:
            current = active_host
            if current is None or current["token"] != token:
                return
            current["downloading"] = False
            current["expires_at"] = asyncio.get_running_loop().time() + DISCORD_EMBED_GRACE_SECONDS
            message_id = current.get("discord_message_id")
            channel_id = current.get("discord_channel_id")

        preview_posted = False
        if output.exists():
            retry_deadline = asyncio.get_running_loop().time() + 10 * 60
            while asyncio.get_running_loop().time() < retry_deadline:
                try:
                    channel = bot.get_channel(channel_id) if channel_id else None
                    if channel is None and channel_id:
                        fetched = await bot.fetch_channel(channel_id)
                        channel = fetched if isinstance(fetched, discord.TextChannel) else None

                    if channel is not None:
                        direct_media_url = f"{PUBLIC_BASE_URL}/cdn/{token}.mp4"

                        final_embed = discord.Embed(
                            title=f"Now Playing: {output.stem}",
                            description=f"[▶ Watch Movie]({direct_media_url})",
                            color=discord.Color.blurple(),
                        )

                        if message_id:
                            try:
                                old_message = await channel.fetch_message(message_id)
                                await old_message.edit(
                                    content=f"▶ **Now Playing:** {output.stem}",
                                    embed=final_embed,
                                    suppress_embeds=False,
                                )
                            except discord.NotFound:
                                await channel.send(
                                    content=f"▶ **Now Playing:** {output.stem}",
                                    embed=final_embed,
                                    suppress_embeds=False,
                                )
                        else:
                            await channel.send(
                                content=f"▶ **Now Playing:** {output.stem}",
                                embed=final_embed,
                                suppress_embeds=False,
                            )

                        preview_posted = True
                        logger.info(
                            "Updated movie embed with final title and direct MP4 URL: %s",
                            token,
                        )
                        break

                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning(
                        "Could not post direct Discord movie preview for %s; "
                        "keeping host alive and retrying: %s",
                        token,
                        exc,
                    )

                await asyncio.sleep(10)

        if not preview_posted:
            logger.warning(
                "Discord direct MP4 preview was not posted after retries; "
                "keeping movie host alive for the full grace period: %s",
                token,
            )

        await asyncio.sleep(DISCORD_EMBED_GRACE_SECONDS)
        await clear_hosted_movie(token, reason="completed movie expired")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("M3U8 streaming task failed: %s", exc)
        await clear_hosted_movie(token, reason="M3U8 streaming task failed")


@movie_group.command(name="play", description="Send a local movie or download an M3U8 movie.")
@app_commands.describe(
    movie="Movie name, or an HTTP/HTTPS .m3u8 URL.",
    name="Display name to use while the M3U8 movie is downloading (optional).",
)
async def movie_play(interaction: discord.Interaction, movie: str, name: str | None = None) -> None:
    if is_m3u8_url(movie):
        display_name = (name or "").strip()[:120] or None
        await interaction.response.defer(ephemeral=True)

        async def progress(message: str) -> None:
            try:
                await interaction.edit_original_response(content=message)
            except discord.HTTPException:
                pass

        # Use the supplied name immediately so the Discord message never has to
        # display a generic M3U8 filename such as index-f2-v1-a1.
        if display_name:
            await progress(f"Preparing **{display_name}**...")

        success, player_url, downloaded = await stream_m3u8_movie(movie, progress)

        if not success or player_url is None:
            await interaction.followup.send(
                "I could not start that M3U8 movie.",
                ephemeral=True,
            )
            return

        channel = await get_movie_channel()
        if channel is None:
            await clear_hosted_movie(active_host["token"] if active_host else "")
            await interaction.followup.send(
                "The movie stream started, but the configured movie channel is unavailable.",
                ephemeral=True,
            )
            return

        movie_name = display_name or (downloaded.stem if downloaded is not None else "M3U8 Movie")
        embed = discord.Embed(
            title=f"Now Playing: {movie_name}",
            description=(
                "Your movie is ready to watch.\n\n"
                f"[▶ Watch Movie]({player_url})"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Format", value="MP4", inline=True)
        embed.add_field(name="Playback", value="Streaming", inline=True)
        embed.set_footer(
            text="The embed player will be sent after the movie is done downloading. "
                 "The link expires automatically when the movie finishes."
        )

        try:
            posted_message = await channel.send(
                content=(
                    f"▶ **Now Playing:** {movie_name}\n"
                    f"{player_url}"
                ),
                embed=embed,
                suppress_embeds=False,
            )
            async with host_lock:
                current = active_host
                if current is not None:
                    current["discord_message_id"] = posted_message.id
                    current["discord_channel_id"] = channel.id
            await interaction.followup.send(
                f"Now streaming {movie_name} in {channel.mention}.",
                ephemeral=True,
            )
        except (discord.Forbidden, discord.HTTPException):
            await clear_hosted_movie(active_host["token"] if active_host else "")
            await interaction.followup.send(
                "The movie stream started, but I could not post the movie player.",
                ephemeral=True,
            )
        return


    if not get_movie_files():
        await interaction.response.send_message(
            "No movies are currently available.",
            ephemeral=True,
        )
        return

    selected = find_movie(movie)
    if selected is None:
        await interaction.response.send_message(
            "That movie is not available. Use /movie list or provide an HTTP/HTTPS .m3u8 URL.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    async def progress(message: str) -> None:
        try:
            await interaction.edit_original_response(content=message)
        except discord.HTTPException:
            pass

    await progress("Embedding movie...")
    success, message = await host_movie(selected, progress)
    if not success:
        await interaction.followup.send(message, ephemeral=True)
        return

    channel = await get_movie_channel()
    if channel is None:
        await clear_hosted_movie(active_host["token"] if active_host else "")
        await interaction.followup.send("The configured movie channel is unavailable.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Now Playing: {selected.stem}",
        description=(
            "Your movie is ready to watch.\n\n"
            f"[▶ Watch Movie]({message})"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Format",
        value=selected.suffix.lower().lstrip(".").upper(),
        inline=True,
    )
    embed.add_field(name="Playback", value="Streaming", inline=True)
    embed.set_footer(text="This movie link expires automatically when the movie ends.")
    try:
        await channel.send(
            content=f"▶ **Now Playing:** {selected.stem}\n{message}",
            embed=embed,
            suppress_embeds=False,
        )
        await interaction.followup.send(f"Now hosting {selected.stem} in {channel.mention}.", ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        await clear_hosted_movie(active_host["token"] if active_host else "")
        await interaction.followup.send("I could not post the movie player in the configured channel.", ephemeral=True)


bot.tree.add_command(channel_group)
bot.tree.add_command(movie_group)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need administrator permissions to use that command."
    elif isinstance(error, app_commands.CommandInvokeError):
        logger.error("Command error: %r", error.original)
        message = "Something went wrong while processing that command."
    else:
        logger.warning("Slash command error: %s", error)
        message = "Something went wrong while processing that command."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def main() -> None:
    ensure_movies_dir()

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is missing. Create a .env file with DISCORD_TOKEN=your_bot_token_here."
        )

    try:
        bot.run(token)
    except discord.LoginFailure:
        raise SystemExit("Discord rejected the bot token. Check your DISCORD_TOKEN.") from None


if __name__ == "__main__":
    main()
