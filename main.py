import asyncio
import asyncio
import sys
import json
import logging
import os
import re
import secrets
import shutil
from pathlib import Path
from urllib.parse import urlparse, urljoin
from html import escape

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from discord.http import handle_message_parameters
from dotenv import load_dotenv

try:
    import wavelink
except ImportError:
    wavelink = None

from lavalink_manager import (
    LAVALINK_URI,
    LAVALINK_PASSWORD,
    keep_lavalink_awake,
    start_lavalink,
    stop_lavalink,
    wait_until_ready as wait_for_lavalink,
)
from youtube_extractor import extract_youtube_audio

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
MOVIES_DIR = BASE_DIR / "Movies"
CONFIG_FILE = BASE_DIR / "config.json"
SUPPORTED_EXTENSIONS = {".mp4", ".mp3"}
M3U8_SUFFIX = ".m3u8"
DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
HOST_EXPIRY_BUFFER_SECONDS = 30
WEB_HOST = os.getenv("MOVIE_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("MOVIE_PORT", "8080"))
HLS_CACHE_DIR = BASE_DIR / ".movie_hls"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://wisp.uno").strip().rstrip("/")
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

async def acknowledge_command(interaction: discord.Interaction) -> None:
    """Acknowledge a slash command immediately so long-running work cannot expire it."""
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException as exc:
        if exc.status == 400 and "40060" in str(exc):
            return
        raise


async def send_interaction_response(
    interaction: discord.Interaction,
    *args,
    **kwargs,
):
    """Send an initial interaction response or a follow-up if already acknowledged."""
    if interaction.response.is_done():
        return await interaction.followup.send(*args, **kwargs)
    return await interaction.response.send_message(*args, **kwargs)


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
            await send_interaction_response(interaction, 
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
            await send_interaction_response(interaction, 
                "That movie is no longer available. Use /movie list to refresh the list.",
                ephemeral=True,
            )
            return

        view = MovieConfirmView(selected, self.owner_id)
        await send_interaction_response(interaction, 
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
            await send_interaction_response(interaction, 
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

        try:
            media_url = f"{PUBLIC_BASE_URL}/media/{active_host['token']}" if active_host else message
            await channel.send(
                content=f"▶ **Now Playing:** {self.movie.stem}\n{media_url}"
            )
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

        await send_interaction_response(interaction, 
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
        ffmpeg_process = current.get("ffmpeg_process")
        stderr_task = current.get("ffmpeg_stderr_task")
        progress_task = current.get("progress_task")
        expiry_task = current.get("expiry_task")
        hls_dir = current.get("hls_dir")
        active_host = None

        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if stderr_task is not None and stderr_task is not asyncio.current_task():
            stderr_task.cancel()
        if progress_task is not None and progress_task is not asyncio.current_task():
            progress_task.cancel()
        if expiry_task is not None and expiry_task is not asyncio.current_task():
            expiry_task.cancel()

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
                "-hls_playlist_type",                "vod",
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

            # An M3U8 movie is playable before its MP4 file is complete.
            # During that phase the HLS cache is the source of truth.
            if not resolved_movie.is_file():
                if not current.get("downloading"):
                    return None
                ffmpeg_process = current.get("ffmpeg_process")
                if ffmpeg_process is None or ffmpeg_process.returncode is not None:
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
<meta property="og:title" content="Watch {title}">
<meta property="og:description" content="Watch {title} directly in your browser.">
<meta property="og:type" content="video.other">
<meta property="og:url" content="{PUBLIC_BASE_URL}/movie/{token}">
<meta property="og:video" content="{PUBLIC_BASE_URL}/media/{token}">
<meta property="og:video:secure_url" content="{PUBLIC_BASE_URL}/media/{token}">
<meta property="og:video:type" content="video/mp4">
<meta property="og:video:url" content="{PUBLIC_BASE_URL}/media/{token}">
<meta property="og:video:duration" content="0">
<meta property="og:video:width" content="1280">
<meta property="og:video:height" content="720">
<meta name="twitter:card" content="player">
<meta name="twitter:title" content="Watch {title}">
<style>
body {{
    margin: 0;
    min-height: 100vh;
    background: #101114;
    color: #fff;
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
main {{
    width: min(1200px, calc(100% - 32px));
    margin: 0 auto;
    padding: 28px 0;
}}
h1 {{
    margin: 0 0 18px;
    font-size: clamp(24px, 4vw, 38px);
}}
.player {{
    background: #000;
    border-radius: 14px;
    overflow: hidden;
    box-shadow: 0 18px 50px rgba(0,0,0,.35);
}}
#status {{
    margin-top: 14px;
    color: #aaa;
}}
</style>
</head>
<body>
<main>
<h1>{title}</h1>
<div class="player">{player}</div>
<div id="status">Loading movie stream...</div>
</main>
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
        if resolved.parent != MOVIES_DIR.resolve() or not resolved.is_file():
            return web.Response(status=404, text="Movie is not ready yet.")
    except OSError:
        return web.Response(status=404, text="Movie is not ready yet.")

    # aiohttp's FileResponse handles byte ranges, which Discord and browsers
    # need when probing/streaming large MP4 files.
    return web.FileResponse(
        path=resolved,
        headers={
            "Content-Type": "video/mp4",
            "Content-Disposition": f'inline; filename="{escape(movie.name)}"',
            "Cache-Control": "public, max-age=0, must-revalidate",
            "Accept-Ranges": "bytes",
            "Access-Control-Allow-Origin": "*",
        },
    )

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


async def youtube_oauth_callback(request: web.Request) -> web.Response:
    state = request.query.get("state", "")
    code = request.query.get("code", "")
    oauth_error = request.query.get("error", "")

    state_info = youtube_oauth_states.pop(state, None)
    if not state_info:
        return web.Response(
            text="This YouTube login link is invalid or has expired.",
            status=400,
            content_type="text/plain",
        )

    created = float(state_info.get("created", 0))
    if asyncio.get_running_loop().time() - created > 600:
        return web.Response(
            text="This YouTube login link expired. Run /login youtube again.",
            status=400,
            content_type="text/plain",
        )

    if oauth_error:
        return web.Response(
            text=f"YouTube login was cancelled or denied: {oauth_error}",
            status=400,
            content_type="text/plain",
        )

    if not code:
        return web.Response(
            text="Google did not return an authorization code.",
            status=400,
            content_type="text/plain",
        )

    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("YOUTUBE_OAUTH_REDIRECT_URI", "").strip()

    if not client_id or not client_secret or not redirect_uri:
        return web.Response(
            text="YouTube OAuth is not configured on the bot.",
            status=500,
            content_type="text/plain",
        )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                token_data = await response.json(content_type=None)

            if response.status != 200 or not isinstance(token_data, dict):
                logger.warning("Google OAuth token exchange failed: HTTP %s", response.status)
                return web.Response(
                    text="Google could not complete the YouTube login.",
                    status=502,
                    content_type="text/plain",
                )

            access_token = str(token_data.get("access_token", "")).strip()
            refresh_token = str(token_data.get("refresh_token", "")).strip()

            if not access_token:
                return web.Response(
                    text="Google did not return an access token.",
                    status=502,
                    content_type="text/plain",
                )

            async with session.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as user_response:
                user_data = await user_response.json(content_type=None)

        token_file = BASE_DIR / "youtube-oauth.json"
        existing = {}
        if token_file.exists():
            try:
                existing = json.loads(token_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}

        existing.update(
            {
                "user_id": state_info["user_id"],
                "access_token": access_token,
                "token_type": token_data.get("token_type", "Bearer"),
                "expires_in": token_data.get("expires_in"),
                "scope": token_data.get("scope", ""),
                "updated_at": int(asyncio.get_running_loop().time()),
            }
        )
        if refresh_token:
            existing["refresh_token"] = refresh_token

        temp_file = token_file.with_suffix(".json.tmp")
        temp_file.write_text(
            json.dumps(existing, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_file.replace(token_file)

        email = ""
        if isinstance(user_data, dict):
            email = str(user_data.get("email", "")).strip()

        logger.info(
            "YouTube Google OAuth account linked for Discord user %s%s.",
            state_info["user_id"],
            f" ({email})" if email else "",
        )

        return web.Response(
            text=(
                "YouTube login successful.\n\n"
                "Your Google/YouTube account is now linked to Movie-Bot. "
                "You can close this page and return to Discord."
            ),
            content_type="text/plain",
        )

    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
        logger.warning("YouTube OAuth callback failed: %s", exc)
        return web.Response(
            text="The YouTube login could not be completed. Please try again.",
            status=502,
            content_type="text/plain",
        )


async def start_movie_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/movie/{token}", hosted_movie_handler)
    app.router.add_get("/media/{token}", hosted_media_handler)
    app.router.add_get("/parts/{token}/{filename}", hosted_part_handler)
    app.router.add_get("/hls/{token}/{filename}", hosted_hls_handler)
    app.router.add_get("/oauth/youtube/callback", youtube_oauth_callback)
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
            await send_interaction_response(interaction, 
                "This channel selector belongs to the administrator who opened it.",
                ephemeral=True,
            )
            return

        selected_channel = self.channel_select.values[0]

        # Resolve the selected channel by ID so Discord's ChannelSelect
        # response works consistently across channel object types.
        channel_id = getattr(selected_channel, "id", None)
        if not isinstance(channel_id, int):
            await send_interaction_response(interaction, 
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
            await send_interaction_response(interaction, 
                "Please select a text channel.",
                ephemeral=True,
            )
            return

        config["channel_id"] = channel.id
        try:
            save_config(config)
        except OSError as exc:
            logger.error("Could not save config: %s", exc)
            await send_interaction_response(interaction, 
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
    await acknowledge_command(interaction)
    await send_interaction_response(interaction, 
        "Choose the Discord text channel where movies should be sent:",
        view=ChannelLinkView(interaction.user.id),
        ephemeral=True,
    )


@movie_group.command(name="list", description="Privately list all available movies.")
async def movie_list(interaction: discord.Interaction) -> None:
    await acknowledge_command(interaction)
    movies = get_movie_files()

    if not movies:
        await send_interaction_response(interaction, 
            "No movies are currently available.",
            ephemeral=True,
        )
        return

    view = MovieListView(movies, interaction.user.id)
    await send_interaction_response(interaction, 
        embed=movie_embed(movies, 0, view.per_page),
        view=view,
        ephemeral=True,
    )



class YouTubeLoginView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id

    async def interaction_allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_response(
            interaction,
            "This YouTube login menu belongs to the member who opened it.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Cookies", style=discord.ButtonStyle.secondary)
    async def cookies_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.interaction_allowed(interaction):
            return
        await send_interaction_response(
            interaction,
            "Use /login youtube again and attach your exported YouTube cookies.txt file in the cookies field. "
            "The file must be in Netscape cookie format.",
            ephemeral=True,
        )

    @discord.ui.button(label="Login", style=discord.ButtonStyle.primary)
    async def login_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.interaction_allowed(interaction):
            return

        client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
        redirect_uri = os.getenv("YOUTUBE_OAUTH_REDIRECT_URI", "").strip()
        if not client_id or not redirect_uri:
            await send_interaction_response(
                interaction,
                "Google YouTube login is not configured. Set GOOGLE_CLIENT_ID and YOUTUBE_OAUTH_REDIRECT_URI first.",
                ephemeral=True,
            )
            return

        state = secrets.token_urlsafe(32)
        youtube_oauth_states[state] = {
            "user_id": interaction.user.id,
            "created": asyncio.get_running_loop().time(),
        }

        from urllib.parse import urlencode
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
            "scope": "https://www.googleapis.com/auth/youtube.readonly",
            "state": state,
        }
        authorization_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)

        await send_interaction_response(
            interaction,
            f"Open Google to log in: {authorization_url}\n\n"
            "After Google finishes, return to Discord. "
            "For yt-dlp playback, you may still need to provide an exported cookies file.",
            ephemeral=True,
        )


async def install_youtube_cookies(
    interaction: discord.Interaction,
    cookies: discord.Attachment,
) -> None:
    filename = Path(cookies.filename or "").name
    if not filename.lower().endswith(".txt"):
        await send_interaction_response(
            interaction,
            "Please upload a .txt cookies export in Netscape format.",
            ephemeral=True,
        )
        return

    if cookies.size > 10 * 1024 * 1024:
        await send_interaction_response(
            interaction,
            "That cookies file is too large. The maximum size is 10 MB.",
            ephemeral=True,
        )
        return

    cookie_path = Path(
        os.getenv("YOUTUBE_COOKIES_FILE", str(BASE_DIR / "youtube-cookies.txt"))
    ).expanduser()

    try:
        data = await cookies.read()
        text_data = data.decode("utf-8-sig", errors="replace")
    except (discord.HTTPException, OSError, UnicodeError) as exc:
        logger.warning("Could not download YouTube cookies attachment: %s", exc)
        await send_interaction_response(
            interaction,
            "I could not read that cookies file from Discord.",
            ephemeral=True,
        )
        return

    lines = [line.strip() for line in text_data.splitlines() if line.strip()]
    if not lines or not any(
        line == "# Netscape HTTP Cookie File"
        or line == "# HTTP Cookie File"
        or line.startswith("# Netscape HTTP Cookie File")
        for line in lines[:5]
    ):
        await send_interaction_response(
            interaction,
            "That file does not look like a Netscape-format YouTube cookies export.",
            ephemeral=True,
        )
        return

    try:
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cookie_path.with_name(cookie_path.name + ".tmp")
        temp_path.write_bytes(data)
        temp_path.replace(cookie_path)
    except OSError as exc:
        logger.error("Could not save YouTube cookies: %s", exc)
        await send_interaction_response(
            interaction,
            "I could not save the YouTube cookies on the bot host.",
            ephemeral=True,
        )
        return

    logger.info("YouTube cookies updated by Discord user %s from %s.", interaction.user.id, filename)
    await send_interaction_response(
        interaction,
        "YouTube cookies were imported successfully. The music extractor will use them for future /play requests.",
        ephemeral=True,
    )


login_group = app_commands.Group(
    name="login",
    description="Manage the account used by the music system.",
)


@login_group.command(
    name="youtube",
    description="Choose YouTube cookies or Google login for the music system.",
)
@app_commands.describe(
    cookies="Optional exported YouTube cookies.txt file in Netscape format.",
)
async def youtube_login(
    interaction: discord.Interaction,
    cookies: discord.Attachment | None = None,
) -> None:
    await acknowledge_command(interaction)

    if interaction.guild is None:
        await send_interaction_response(
            interaction,
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    if not interaction.user.guild_permissions.administrator:
        await send_interaction_response(
            interaction,
            "You need administrator permissions to configure the bot's YouTube account.",
            ephemeral=True,
        )
        return

    if cookies is not None:
        await install_youtube_cookies(interaction, cookies)
        return

    await send_interaction_response(
        interaction,
        "YouTube Account Login\nChoose how to connect the YouTube account used by the music system.",
        view=YouTubeLoginView(interaction.user.id),
        ephemeral=True,
    )


class MovieBot(discord.Client):
    def __init__(self) -> None:
        self.movie_web_runner: web.AppRunner | None = None
        self.lavalink_keepalive_task: asyncio.Task | None = None
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        self.movie_web_runner = await start_movie_web_server()

        # Render free web services can suspend after a period without inbound
        # HTTP traffic. Start the keep-alive before connecting so a sleeping
        # Lavalink service is woken while initialize_lavalink waits for it.
        if LAVALINK_URI and LAVALINK_PASSWORD:
            self.lavalink_keepalive_task = asyncio.create_task(
                keep_lavalink_awake(),
                name="lavalink-render-keepalive",
            )

        await initialize_lavalink()

        try:
            # Remove the temporary guild-scoped copies created by an earlier
            # sync strategy. The bot now uses global application commands only.
            for guild in self.guilds:
                try:
                    self.tree.clear_commands(guild=guild)
                    await self.tree.sync(guild=guild)
                    logger.info("Cleared old guild-scoped commands from %s.", guild.name)
                except discord.HTTPException as exc:
                    logger.warning("Could not clear old guild commands from %s: %s", guild.name, exc)

            synced = await self.tree.sync()
            logger.info("Synced %d global slash command(s).", len(synced))
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
        for guild_id in list(music_states):
            await cancel_lyrics(guild_id)
        if self.lavalink_keepalive_task is not None:
            self.lavalink_keepalive_task.cancel()
            try:
                await self.lavalink_keepalive_task
            except asyncio.CancelledError:
                pass
            self.lavalink_keepalive_task = None

        if wavelink is not None:
            try:
                await wavelink.Pool.close()
            except Exception as exc:
                logger.warning("Could not close Lavalink cleanly: %s", exc)
        await stop_lavalink()
        await stop_cloudflare_quick_tunnel()
        if self.movie_web_runner is not None:
            await self.movie_web_runner.cleanup()
            self.movie_web_runner = None
        await super().close()

    async def on_ready(self) -> None:
        if self.user:
            logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)

        # Remove stale guild-scoped command copies left by the old sync
        # strategy. Commands are now registered globally only.
        for guild in self.guilds:
            try:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info(
                    "Cleared old guild-scoped commands from %s (%s).",
                    guild.name,
                    guild.id,
                )
            except discord.HTTPException as exc:
                logger.warning(
                    "Could not clear old guild commands from %s (%s): %s",
                    guild.name,
                    guild.id,
                    exc,
                )


bot = MovieBot()


# -------------------------
# Voice / music playback
# -------------------------

LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"

# Lavalink runs separately. Set LAVALINK_URI and LAVALINK_PASSWORD in the
# hosting environment to match the external Lavalink server.
music_states: dict[int, dict] = {}
lavalink_ready = False
youtube_oauth_states: dict[str, dict] = {}


def get_music_state(guild_id: int) -> dict:
    state = music_states.get(guild_id)
    if state is None:
        state = {
            "queue": [],
            "player": None,
            "current": None,
            "volume": 100,
            "lyrics_task": None,
            "lyrics_enabled": False,
            "advance_lock": asyncio.Lock(),
            "voice_lock": asyncio.Lock(),
            "stopping": False,
        }
        music_states[guild_id] = state
    return state


def lavalink_uri() -> str:
    return LAVALINK_URI


async def initialize_lavalink() -> None:
    global lavalink_ready
    if wavelink is None:
        logger.error("Wavelink is not installed; music playback is unavailable.")
        return

    try:
        # Lavalink runs on a separate Java server. The Python container only
        # connects to it and never downloads or starts a JVM.
        await start_lavalink(LAVALINK_PASSWORD)
        await wait_for_lavalink()

        nodes = await wavelink.Pool.connect(
            nodes=[
                wavelink.Node(
                    identifier="local",
                    uri=lavalink_uri(),
                    password=LAVALINK_PASSWORD,
                    retries=None,
                    resume_timeout=60,
                )
            ],
            client=bot,
            cache_capacity=100,
        )
        lavalink_ready = bool(nodes)
        if lavalink_ready:
            logger.info("External Lavalink music backend connected.")
        else:
            logger.warning("External Lavalink did not report a ready Wavelink node.")
    except Exception as exc:
        lavalink_ready = False
        logger.exception("Failed to connect to external Lavalink: %s", exc)


def spotify_track_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.lower() in {"open.spotify.com", "spotify.link"}
        and "/track/" in parsed.path.lower()
    )


async def fetch_json(session, url: str, **kwargs):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20), **kwargs) as response:
            if response.status != 200:
                return None
            return await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        return None


async def resolve_spotify_track(value: str) -> dict | None:
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(
            session,
            "https://open.spotify.com/oembed",
            params={"url": value.strip()},
        )
    if not isinstance(data, dict):
        return None
    title = str(data.get("title", "")).strip()
    artist = str(data.get("author_name", "")).strip()
    if not title:
        return None
    return {"title": title, "artist": artist or "Unknown Artist", "spotify_url": value.strip()}


async def resolve_music_source(query: str) -> dict | None:
    """Resolve a song and hand Lavalink a direct, short-lived audio URL.

    YouTube's current playback API can return SABR-only formats that the
    youtube-source plugin cannot always turn into a playable Lavalink track.
    We therefore use yt-dlp as an extractor and let Lavalink's HTTP source
    handle the resulting audio URL. YouTube authentication/cookies stay in
    the extractor environment and are never sent to Discord.
    """
    if not lavalink_ready or wavelink is None:
        return None

    is_spotify = spotify_track_url(query)
    spotify_info = await resolve_spotify_track(query) if is_spotify else None
    if is_spotify and spotify_info is None:
        return None

    if spotify_info:
        search_term = f"{spotify_info['artist']} - {spotify_info['title']}"
    else:
        search_term = query.strip()
        if not search_term:
            return None

    try:
        extracted = await extract_youtube_audio(search_term)
    except Exception as exc:
        logger.warning("YouTube extractor failed for %r: %s", search_term, exc)
        return None

    if not extracted or not extracted.get("url"):
        return None

    audio_url = str(extracted["url"])
    try:
        results = await wavelink.Pool.fetch_tracks(audio_url)
    except Exception as exc:
        logger.warning("Lavalink could not load extracted audio for %r: %s", search_term, exc)
        return None

    if not results:
        return None

    tracks = list(results.tracks) if isinstance(results, wavelink.Playlist) else list(results)
    if not tracks:
        return None

    playable = tracks[0]
    title = str(extracted.get("title") or getattr(playable, "title", "") or "").strip()
    if not title:
        return None

    artist = (
        str(spotify_info["artist"]).strip()
        if spotify_info
        else str(extracted.get("artist") or getattr(playable, "author", "") or "Unknown Artist").strip()
    )

    return {
        "track": playable,
        "title": title,
        "artist": artist or "Unknown Artist",
        "duration": max(
            0,
            int(
                extracted.get("duration_ms")
                or getattr(playable, "length", 0)
                or 0
            ),
        ),
        "spotify_url": spotify_info.get("spotify_url") if spotify_info else None,
        "youtube_url": extracted.get("webpage_url"),
        "lyrics": [],
    }


async def fetch_lyrics(track: dict) -> list[tuple[float, str]]:
    artist = str(track.get("artist", "")).strip()
    title = str(track.get("title", "")).strip()
    if not title:
        return []

    async with aiohttp.ClientSession() as session:
        exact = await fetch_json(
            session,
            "https://lrclib.net/api/get",
            params={"artist_name": artist, "track_name": title},
        )
        candidates = [exact] if isinstance(exact, dict) else []
        if not candidates or not str(candidates[0].get("syncedLyrics") or "").strip():
            data = await fetch_json(
                session,
                LRCLIB_SEARCH_URL,
                params={"q": f"{artist} {title}"},
            )
            if isinstance(data, list):
                candidates.extend(item for item in data if isinstance(item, dict))

    artist_key = re.sub(r"[^a-z0-9]+", "", artist.casefold())
    title_key = re.sub(r"[^a-z0-9]+", "", title.casefold())

    def lyric_score(item: dict) -> int:
        item_artist = re.sub(r"[^a-z0-9]+", "", str(item.get("artistName") or "").casefold())
        item_title = re.sub(r"[^a-z0-9]+", "", str(item.get("trackName") or "").casefold())
        score = 0
        if item_title == title_key:
            score += 20
        elif title_key and title_key in item_title:
            score += 10
        if item_artist == artist_key:
            score += 20
        elif artist_key and artist_key in item_artist:
            score += 10
        if str(item.get("syncedLyrics") or "").strip():
            score += 30
        return score

    candidates.sort(key=lyric_score, reverse=True)
    best = next((item for item in candidates if str(item.get("syncedLyrics") or "").strip()), None)
    if best is None:
        return []

    timestamp_re = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
    lyrics: list[tuple[float, str]] = []
    for raw_line in str(best.get("syncedLyrics") or "").splitlines():
        matches = list(timestamp_re.finditer(raw_line))
        text_line = timestamp_re.sub("", raw_line).strip()
        if not text_line:
            continue
        for match in matches:
            try:
                lyrics.append((int(match.group(1)) * 60 + float(match.group(2)), text_line))
            except ValueError:
                continue
    lyrics.sort(key=lambda item: item[0])
    return lyrics


async def send_voice_chat_message(voice_channel: discord.abc.GuildChannel, content: str):
    try:
        with handle_message_parameters(
            content=content,
            allowed_mentions=discord.AllowedMentions.none(),
        ) as params:
            return await bot.http.send_message(voice_channel.id, params=params)
    except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
        return None


async def lyrics_loop(
    guild_id: int,
    player,
    voice_channel,
    track: dict,
    start_from_position: bool = False,
) -> None:
    lyrics = track.get("lyrics") or []
    if not lyrics:
        return

    last_index = -1
    if start_from_position:
        position_seconds = max(0, player.position) / 1000.0
        for index, (timestamp, _) in enumerate(lyrics):
            if timestamp <= position_seconds:
                last_index = index
            else:
                break

    try:
        while True:
            state = music_states.get(guild_id)
            if state is None or state.get("current") is not track:
                return
            if not state.get("lyrics_enabled", False):
                await asyncio.sleep(0.5)
                continue

            position_seconds = max(0, player.position) / 1000.0
            while last_index + 1 < len(lyrics) and lyrics[last_index + 1][0] <= position_seconds:
                last_index += 1
                state = music_states.get(guild_id)
                if state is None or state.get("current") is not track:
                    return
                await send_voice_chat_message(voice_channel, f"**{lyrics[last_index][1]}**")
            await asyncio.sleep(0.20 if player.playing else 0.25)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Synchronized lyrics stopped for guild %s: %s", guild_id, exc)


async def cancel_lyrics(guild_id: int) -> None:
    state = music_states.get(guild_id)
    if state is None:
        return
    task = state.get("lyrics_task")
    state["lyrics_task"] = None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def connect_member_voice(interaction: discord.Interaction):
    if interaction.guild is None or wavelink is None:
        return None

    member = interaction.user
    if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
        return None

    voice_channel = member.voice.channel
    state = get_music_state(interaction.guild.id)

    # Serialize voice connection attempts. Two /play or /join commands arriving
    # together must never race and create a second Discord voice connection.
    async with state["voice_lock"]:
        player = state.get("player")

        # Recover an existing Wavelink player if the in-memory music state lost
        # its reference after a reconnect or another command created the player.
        if player is None:
            for existing in bot.voice_clients:
                if getattr(existing, "guild", None) == interaction.guild:
                    if isinstance(existing, wavelink.Player):
                        player = existing
                        state["player"] = existing
                        logger.info(
                            "Recovered existing Lavalink player for guild %s.",
                            interaction.guild.id,
                        )
                    break

        if player is not None:
            try:
                if player.connected:
                    if player.channel is not None and player.channel.id != voice_channel.id:
                        await player.move_to(voice_channel)
                    return player
                await player.disconnect()
            except Exception as exc:
                logger.warning(
                    "Existing Lavalink player for guild %s was stale: %s",
                    interaction.guild.id,
                    exc,
                )
            state["player"] = None
            player = None

        try:
            player = await voice_channel.connect(cls=wavelink.Player, self_deaf=True)
        except Exception as exc:
            # A voice client can have appeared between our lookup and connect.
            # Recover it instead of reporting the duplicate-connection error.
            for existing in bot.voice_clients:
                if (
                    getattr(existing, "guild", None) == interaction.guild
                    and isinstance(existing, wavelink.Player)
                ):
                    player = existing
                    state["player"] = existing
                    logger.info(
                        "Recovered Lavalink player after duplicate connection "
                        "race for guild %s.",
                        interaction.guild.id,
                    )
                    try:
                        if player.connected and player.channel is not None and player.channel.id != voice_channel.id:
                            await player.move_to(voice_channel)
                    except Exception:
                        pass
                    return player

            logger.warning(
                "Could not connect Lavalink player to guild %s: %s",
                interaction.guild.id,
                exc,
            )
            return None

        state["player"] = player
        return player


async def play_next(guild_id: int) -> bool:
    state = get_music_state(guild_id)
    player = state.get("player")
    if player is None:
        return False

    async with state["advance_lock"]:
        if state.get("stopping"):
            return False
        while state["queue"]:
            player = state.get("player")
            if player is None or not player.connected:
                return False
            if player.playing or state.get("current") is not None:
                return False

            track = state["queue"].pop(0)
            state["current"] = track
            try:
                track["lyrics"] = await fetch_lyrics(track)
                await player.play(track["track"], volume=int(state.get("volume", 100)))
                return True
            except Exception as exc:
                logger.warning("Lavalink playback failed for %s: %s", track.get("title", "unknown"), exc)
                state["current"] = None
    return False


@bot.tree.command(name="join", description="Join your current voice channel.")
async def voice_join(interaction: discord.Interaction) -> None:
    await acknowledge_command(interaction)
    if not lavalink_ready:
        await send_interaction_response(interaction, "The music backend is currently unavailable.", ephemeral=True)
        return
    player = await connect_member_voice(interaction)
    if player is None:
        await send_interaction_response(interaction, "Join a voice channel first, then use /join.", ephemeral=True)
        return
    await send_interaction_response(interaction, f"Joined **{player.channel.name}**.", ephemeral=True)


@bot.tree.command(name="leave", description="Leave the voice channel and clear the music queue.")
async def voice_leave(interaction: discord.Interaction) -> None:
    await acknowledge_command(interaction)
    if interaction.guild is None:
        await send_interaction_response(interaction, "This command only works in a server.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    player = state.get("player")
    if player is None or not player.connected:
        await send_interaction_response(interaction, "I am not in a voice channel.", ephemeral=True)
        return

    state["stopping"] = True
    state["queue"].clear()
    await cancel_lyrics(interaction.guild.id)
    try:
        await player.disconnect()
    except Exception as exc:
        logger.warning("Lavalink player disconnect failed for guild %s: %s", interaction.guild.id, exc)
    state["player"] = None
    state["current"] = None
    state["stopping"] = False
    await send_interaction_response(interaction, "Left the voice channel and cleared the queue.", ephemeral=True)


@bot.tree.command(name="sync", description="Sync all slash commands to this server.")
@app_commands.checks.has_permissions(administrator=True)
async def sync_commands(interaction: discord.Interaction) -> None:
    await acknowledge_command(interaction)
    if interaction.guild is None:
        await send_interaction_response(interaction, 
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    try:
        synced = await bot.tree.sync()
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.warning("Could not sync global commands: %s", exc)
        await interaction.followup.send(
            "I could not sync the commands. Please try again later.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"Synced {len(synced)} global slash command(s).",
        ephemeral=True,
    )


bot.tree.add_command(channel_group)
bot.tree.add_command(movie_group)

# Remove any stale top-level /login command/group left by an older version,
# then register exactly one current /login group.
bot.tree.remove_command("login")
bot.tree.add_command(login_group)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, discord.NotFound) and getattr(error, "code", None) == 10062:
        # Discord can invalidate an interaction before the bot gets a chance
        # to acknowledge it. This is not recoverable with another response.
        logger.warning("Discord interaction expired before it could be acknowledged (10062).")
        return

    try:
        await acknowledge_command(interaction)
    except discord.NotFound as response_error:
        if getattr(response_error, "code", None) == 10062:
            logger.warning("Discord interaction expired while handling a slash command error (10062).")
            return
        raise

    if isinstance(error, app_commands.MissingPermissions):
        message = "You need administrator permissions to use that command."
    elif isinstance(error, app_commands.CommandInvokeError):
        original = error.original
        if isinstance(original, discord.HTTPException) and original.status == 400 and "40060" in str(original):
            # Discord has already acknowledged this interaction. Do not attempt
            # another response, which would only produce a second 40060 error.
            logger.warning("Command interaction was already acknowledged: %r", original)
            return
        logger.error("Command error: %r", original)
        message = "Something went wrong while processing that command."
    else:
        logger.warning("Slash command error: %s", error)
        message = "Something went wrong while processing that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await send_interaction_response(interaction, message, ephemeral=True)
    except (discord.NotFound, discord.HTTPException) as response_error:
        # The interaction token can expire while a long-running command is
        # failing. Do not create a second traceback for the error handler.
        logger.debug("Could not send command error response: %s", response_error)


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
