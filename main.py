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


async def start_movie_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/movie/{token}", hosted_movie_handler)
    app.router.add_get("/media/{token}", hosted_media_handler)
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


# -------------------------
# Voice / music playback
# -------------------------

MUSIC_CACHE_DIR = BASE_DIR / ".music_cache"
SPOTIFY_OEMBED_URL = "https://open.spotify.com/oembed"
LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"

music_states: dict[int, dict] = {}


def get_music_state(guild_id: int) -> dict:
    state = music_states.get(guild_id)
    if state is None:
        state = {
            "queue": [],
            "voice": None,
            "current": None,
            "volume": 1.0,
            "play_task": None,
            "lyrics_task": None,
            "lyrics_enabled": True,
        }
        music_states[guild_id] = state
    return state


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
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return None


async def resolve_spotify_track(value: str) -> dict | None:
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(session, SPOTIFY_OEMBED_URL, params={"url": value.strip()})

    if not isinstance(data, dict):
        return None

    title = str(data.get("title", "")).strip()
    artist = str(data.get("author_name", "")).strip()
    if not title:
        return None

    return {
        "title": title,
        "artist": artist or "Unknown Artist",
        "search": f"{artist} - {title}".strip(" -"),
        "spotify_url": value.strip(),
    }


async def run_yt_dlp(args: list[str], timeout: int = 180) -> tuple[int, str, str]:
    # Run yt-dlp through the exact Python interpreter hosting the bot.
    # Explicitly select Deno for YouTube's current JavaScript challenge
    # solver. Render installs Deno during the image build, but yt-dlp will
    # not automatically use it unless the runtime is explicitly selected.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "yt_dlp",
        "--js-runtimes",
        "deno",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise

    return process.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


async def piped_instances() -> list[str]:
    """Return public Piped API instances for music search and audio streams."""
    discovered: list[str] = []

    for endpoint in (
        "https://piped.video/api/v1/instances",
        "https://pipedapi.kavin.rocks/instances",
    ):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    endpoint,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status != 200:
                        continue
                    data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            continue

        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                api_url = str(item.get("api_url") or item.get("apiUrl") or "").strip().rstrip("/")
                if api_url.startswith("https://") and api_url not in discovered:
                    discovered.append(api_url)
        if discovered:
            break

    for api_url in (
        "https://pipedapi.kavin.rocks",
        "https://pipedapi.adminforge.de",
        "https://pipedapi.reallyaweso.me",
    ):
        if api_url not in discovered:
            discovered.append(api_url)

    return discovered[:8]


async def resolve_music_source(query: str) -> dict | None:
    spotify_info = await resolve_spotify_track(query) if spotify_track_url(query) else None
    search_query = spotify_info["search"] if spotify_info else query.strip()
    if not search_query:
        return None

    # YouTube is currently returning HTTP 429 to Render. Piped performs the
    # search through its own API so Render does not have to make YouTube
    # search/player requests directly.
    instances = await piped_instances()
    candidates: list[dict] = []

    async with aiohttp.ClientSession() as session:
        for api_base in instances:
            try:
                async with session.get(
                    f"{api_base}/search",
                    params={"q": search_query, "filter": "music"},
                    headers={"User-Agent": "Movie-Bot/1.0"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        continue
                    data = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
                continue

            if not isinstance(data, list):
                continue

            for entry in data:
                if not isinstance(entry, dict):
                    continue

                video_id = str(entry.get("url") or "").strip()
                if video_id.startswith("/watch?v="):
                    video_id = video_id.split("v=", 1)[1].split("&", 1)[0]
                if not video_id:
                    video_id = str(entry.get("id") or "").strip()
                if not video_id:
                    continue

                artist = str(
                    entry.get("uploaderName")
                    or entry.get("uploader")
                    or ((spotify_info or {}).get("artist") if spotify_info else "")
                    or "Unknown Artist"
                ).strip()

                candidates.append({
                    "title": str(entry.get("title") or "").strip(),
                    "artist": artist,
                    "video_id": video_id,
                    "api_base": api_base,
                    "duration": entry.get("duration"),
                    "spotify_url": (spotify_info or {}).get("spotify_url"),
                    "source": "Piped",
                })

            if candidates:
                break

    if not candidates:
        return None

    requested_title = str((spotify_info or {}).get("title") or "").casefold()
    requested_artist = str((spotify_info or {}).get("artist") or "").casefold()

    def normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.casefold())

    title_key = normalize(requested_title)
    artist_key = normalize(requested_artist)

    def score(entry: dict) -> int:
        title = str(entry.get("title") or "")
        actual_title = normalize(title)
        uploader = normalize(str(entry.get("artist") or ""))
        value = 0

        if title_key and title_key == actual_title:
            value += 30
        elif title_key and title_key in actual_title:
            value += 15
        if artist_key and artist_key in actual_title:
            value += 15
        if artist_key and artist_key in uploader:
            value += 8

        title_lower = title.casefold()
        if "official audio" in title_lower:
            value += 15
        elif "official" in title_lower:
            value += 8
        if "topic" in title_lower or "audio" in title_lower:
            value += 6
        if any(word in title_lower for word in (
            "music video", "official video", "lyrics", "lyric video",
            "visualizer", "edit", "intro", "extended",
        )):
            value -= 12
        if any(word in title_lower for word in (
            "live", "remix", "cover", "8d", "nightcore", "slowed", "sped up",
        )):
            value -= 10
        return value

    candidates.sort(key=score, reverse=True)
    primary = candidates[0]
    primary["alternatives"] = candidates[1:8]
    return primary


async def download_music_audio(track: dict) -> Path | None:
    MUSIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    candidates = [track] + [
        candidate for candidate in track.get("alternatives", [])
        if isinstance(candidate, dict)
    ]

    # Ask the Piped instance for the audio stream. Prefer proxyUrl when the
    # instance supplies one so the actual media request stays on the instance.
    async with aiohttp.ClientSession() as session:
        for attempt, candidate in enumerate(candidates, start=1):
            api_base = str(candidate.get("api_base") or "").rstrip("/")
            video_id = str(candidate.get("video_id") or "").strip()
            if not api_base or not video_id:
                continue

            try:
                async with session.get(
                    f"{api_base}/streams/{video_id}",
                    headers={"User-Agent": "Movie-Bot/1.0"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            "Piped stream lookup failed for %s: HTTP %s",
                            candidate.get("title", "unknown"),
                            response.status,
                        )
                        continue
                    stream_data = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
                logger.warning("Piped stream lookup failed: %s", exc)
                continue

            audio_streams = stream_data.get("audioStreams") if isinstance(stream_data, dict) else None
            if not isinstance(audio_streams, list):
                continue

            def stream_score(item: dict) -> tuple[int, int]:
                fmt = str(item.get("format") or "").casefold()
                codec = str(item.get("codec") or "").casefold()
                try:
                    bitrate = int(item.get("bitrate") or 0)
                except (TypeError, ValueError):
                    bitrate = 0

                value = 0
                if fmt in {"m4a", "mp4"}:
                    value += 30
                elif fmt in {"opus", "webm"}:
                    value += 20
                if "aac" in codec:
                    value += 10
                return value, bitrate

            valid_streams = [
                item for item in audio_streams
                if isinstance(item, dict)
                and str(item.get("proxyUrl") or item.get("proxy_url") or item.get("url") or "").strip()
            ]
            valid_streams.sort(key=stream_score, reverse=True)

            for stream in valid_streams:
                stream_url = str(
                    stream.get("proxyUrl")
                    or stream.get("proxy_url")
                    or stream.get("url")
                    or ""
                ).strip()
                if not stream_url:
                    continue

                extension = str(stream.get("format") or "").casefold()
                if extension not in {"m4a", "mp4", "webm", "opus", "aac", "wav"}:
                    extension = "m4a"

                token = secrets.token_hex(12)
                temp_file = MUSIC_CACHE_DIR / f"{token}.{extension}.part"
                final_file = MUSIC_CACHE_DIR / f"{token}.{extension}"

                try:
                    async with session.get(
                        stream_url,
                        headers={"User-Agent": "Movie-Bot/1.0"},
                        timeout=aiohttp.ClientTimeout(total=15 * 60),
                    ) as response:
                        if response.status not in {200, 206}:
                            logger.warning(
                                "Piped audio download failed for %s: HTTP %s",
                                candidate.get("title", "unknown"),
                                response.status,
                            )
                            continue

                        with temp_file.open("wb") as file:
                            async for chunk in response.content.iter_chunked(1024 * 256):
                                if chunk:
                                    file.write(chunk)

                    if temp_file.exists() and temp_file.stat().st_size > 0:
                        temp_file.replace(final_file)
                        if attempt > 1:
                            logger.info(
                                "Using Piped fallback candidate %d for %s",
                                attempt,
                                candidate.get("title", "unknown"),
                            )
                        return final_file
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    logger.warning(
                        "Piped audio download failed for %s: %s",
                        candidate.get("title", "unknown"),
                        exc,
                    )
                finally:
                    temp_file.unlink(missing_ok=True)

    return None


async def fetch_lyrics(track: dict) -> list[tuple[float, str]]:
    artist = str(track.get("artist", "")).strip()
    title = str(track.get("title", "")).strip()
    if not title:
        return []

    async with aiohttp.ClientSession() as session:
        # LRCLIB's exact lookup is much more reliable than a broad search.
        exact = await fetch_json(
            session,
            "https://lrclib.net/api/get",
            params={
                "artist_name": artist,
                "track_name": title,
            },
        )

        candidates = []
        if isinstance(exact, dict):
            candidates.append(exact)

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
        item_artist = re.sub(
            r"[^a-z0-9]+", "", str(item.get("artistName") or "").casefold()
        )
        item_title = re.sub(
            r"[^a-z0-9]+", "", str(item.get("trackName") or "").casefold()
        )
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
    best = next(
        (item for item in candidates if str(item.get("syncedLyrics") or "").strip()),
        None,
    )
    if best is None:
        return []

    timestamp_re = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
    lyrics: list[tuple[float, str]] = []

    for raw_line in str(best.get("syncedLyrics") or "").splitlines():
        matches = list(timestamp_re.finditer(raw_line))
        text = timestamp_re.sub("", raw_line).strip()
        if not text:
            continue
        for match in matches:
            try:
                timestamp = int(match.group(1)) * 60 + float(match.group(2))
                lyrics.append((timestamp, text))
            except ValueError:
                continue

    lyrics.sort(key=lambda item: item[0])

    # Keep the lyric database duration so playback can automatically skip
    # an extra intro that some YouTube uploads add before the actual song.
    try:
        lyric_duration = float(best.get("duration")) if best.get("duration") is not None else None
    except (TypeError, ValueError):
        lyric_duration = None
    if lyric_duration is not None and lyric_duration > 0:
        track["lyrics_duration"] = lyric_duration

    return lyrics


async def send_voice_chat_message(voice_channel: discord.VoiceChannel, content: str):
    # discord.py does not currently expose VoiceChannel.send() consistently,
    # but Discord's message API supports messages in voice channels. Use the
    # same message-parameter builder as discord.py's Messageable implementation.
    try:
        with handle_message_parameters(
            content=content,
            allowed_mentions=discord.AllowedMentions.none(),
        ) as params:
            return await bot.http.send_message(voice_channel.id, params=params)
    except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
        return None


async def lyrics_loop(guild_id: int, voice_channel: discord.VoiceChannel, track: dict, started_at: float, lyrics: list[tuple[float, str]]) -> None:
    loop = asyncio.get_running_loop()
    for timestamp, line in lyrics:
        wait_for = started_at + timestamp - loop.time()
        if wait_for > 0:
            try:
                await asyncio.sleep(wait_for)
            except asyncio.CancelledError:
                raise

        current = music_states.get(guild_id)
        if current is None or current.get("current") is not track:
            return
        if not current.get("lyrics_enabled", True):
            continue

        try:
            await send_voice_chat_message(voice_channel, f"**{line}**")
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            return


async def connect_member_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    if interaction.guild is None:
        return None

    member = interaction.user
    if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
        return None

    voice_channel = member.voice.channel
    state = get_music_state(interaction.guild.id)
    voice = state.get("voice")

            continue

        finished = asyncio.Event()

        def after_play(error):
            if error:
                logger.warning("Voice playback error: %s", error)
            loop = getattr(bot, "loop", None)
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(finished.set)

        voice.play(source, after=after_play)
        # Start the lyric clock at playback start. Lyrics were fetched before
        # voice.play(), so network latency cannot delay the first line.
        started_at = asyncio.get_running_loop().time() - 0.15
        lyrics_task = (
            asyncio.create_task(
                lyrics_loop(guild_id, voice.channel, track, started_at, lyrics)
            )
            if lyrics and state.get("lyrics_enabled", True) else None
        )
        state["lyrics_task"] = lyrics_task

        try:
            await finished.wait()
        finally:
            if lyrics_task is not None and state.get("lyrics_task") is lyrics_task:
                lyrics_task.cancel()
                try:
                    await lyrics_task
                except asyncio.CancelledError:
                    pass
                state["lyrics_task"] = None

        state["current"] = None

    state["current"] = None


async def ensure_music_player(guild_id: int) -> None:
    state = get_music_state(guild_id)
    task = state.get("play_task")
    if task is None or task.done():
        state["play_task"] = asyncio.create_task(start_track(guild_id))


async def cleanup_music_state(guild_id: int) -> None:
    state = music_states.get(guild_id)
    if state is None:
        return

    lyrics_task = state.get("lyrics_task")
    if lyrics_task is not None:
        lyrics_task.cancel()

    play_task = state.get("play_task")
    if play_task is not None and play_task is not asyncio.current_task():
        play_task.cancel()

    voice = state.get("voice")
    if voice is not None and voice.is_connected():
        try:
            await voice.disconnect()
        except (discord.ClientException, discord.HTTPException):
            pass

    state["voice"] = None
    state["current"] = None
    state["queue"].clear()
    state["play_task"] = None
    state["lyrics_task"] = None


@bot.tree.command(name="join", description="Join your current voice channel.")
async def voice_join(interaction: discord.Interaction) -> None:
    voice = await connect_member_voice(interaction)
    if voice is None:
        await interaction.response.send_message("Join a voice channel first, then use /join.", ephemeral=True)
        return
    await interaction.response.send_message(f"Joined **{voice.channel.name}**.", ephemeral=True)


@bot.tree.command(name="leave", description="Leave the voice channel and clear the music queue.")
async def voice_leave(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    state = get_music_state(interaction.guild.id)
    voice = state.get("voice")
    if voice is None or not voice.is_connected():
        await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)
        return

    await cleanup_music_state(interaction.guild.id)
    await interaction.response.send_message("Left the voice channel and cleared the queue.", ephemeral=True)


@bot.tree.command(name="play", description="Play a song or Spotify track in your voice channel.")
@app_commands.describe(song="A song name, or an open.spotify.com/track URL.")
async def music_play(interaction: discord.Interaction, song: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    voice = await connect_member_voice(interaction)
    if voice is None:
        await interaction.response.send_message("Join a voice channel first. I can then play the song there.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        track = await resolve_music_source(song)
        if track is None:
            await interaction.followup.send(
                "I could not find that song. Spotify track links and normal song searches are supported.",
                ephemeral=True,
            )
            return

        await interaction.edit_original_response(content=f"Downloading **{track['title']}**...")
        path = await download_music_audio(track)
        if path is None:
            await interaction.followup.send(
                "I found the song, but could not download an audio source for playback.",
                ephemeral=True,
            )
            return

        track["path"] = path
        state = get_music_state(interaction.guild.id)
        was_playing = state.get("current") is not None
        state["queue"].append(track)
        position = len(state["queue"])

        await ensure_music_player(interaction.guild.id)

        if was_playing:
            message = f"Queued **{track['title']}** by **{track['artist']}** at position {position}."
        else:
            message = f"Playing **{track['title']}** by **{track['artist']}**."

        await interaction.followup.send(message, ephemeral=True)
    except FileNotFoundError:
        await interaction.followup.send("yt-dlp is not installed. Install the requirements and restart the bot.", ephemeral=True)
    except asyncio.TimeoutError:
        await interaction.followup.send("The song download took too long and was cancelled.", ephemeral=True)
    except Exception as exc:
        logger.exception("Music play failed: %s", exc)
        await interaction.followup.send("Something went wrong while preparing that song.", ephemeral=True)


@bot.tree.command(name="pause", description="Pause the current song.")
async def music_pause(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    voice = state.get("voice")
    if voice is None or not voice.is_playing():
        await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
        return
    voice.pause()
    await interaction.response.send_message("Paused the current song.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the paused song.")
async def music_resume(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    voice = state.get("voice")
    if voice is None or not voice.is_paused():
        await interaction.response.send_message("The song is not paused.", ephemeral=True)
        return
    voice.resume()
    await interaction.response.send_message("Resumed the current song.", ephemeral=True)


@bot.tree.command(name="lyrics", description="Toggle synchronized lyrics on or off.")
@app_commands.describe(enabled="Turn synchronized lyrics on or off. Leave empty to toggle.")
async def music_lyrics(interaction: discord.Interaction, enabled: bool | None = None) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    if enabled is None:
        enabled = not state.get("lyrics_enabled", True)
    state["lyrics_enabled"] = bool(enabled)
    status = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"Synchronized lyrics are now **{status}**.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip the currently playing song.")
async def music_skip(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    state = get_music_state(interaction.guild.id)
    voice = state.get("voice")
    if voice is None or not voice.is_playing():
        await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
        return

    voice.stop()
    await interaction.response.send_message("Skipped the current song.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the current music queue.")
async def music_queue(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    state = get_music_state(interaction.guild.id)
    current = state.get("current")
    queue = state.get("queue", [])
    lines = []

    if current is not None:
        lines.append(f"**Now playing:** {current['title']} — {current['artist']}")
    if queue:
        lines.append("")
        lines.extend(
            f"**{index}.** {track['title']} — {track['artist']}"
            for index, track in enumerate(queue, 1)
        )
    if not lines:
        lines = ["The music queue is empty."]

    await interaction.response.send_message("\n".join(lines[:51]), ephemeral=True)


@bot.tree.command(name="volume", description="Set the music volume.")
@app_commands.describe(level="Volume from 0 to 100.")
async def music_volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    state = get_music_state(interaction.guild.id)
    state["volume"] = int(level) / 100

    voice = state.get("voice")
    if voice is not None and isinstance(voice.source, discord.PCMVolumeTransformer):
        voice.source.volume = state["volume"]

    await interaction.response.send_message(f"Volume set to **{level}%**.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop music and clear the queue.")
async def music_stop(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    state = get_music_state(interaction.guild.id)
    state["queue"].clear()
    voice = state.get("voice")
    if voice is not None and voice.is_playing():
        voice.stop()
    await interaction.response.send_message("Stopped playback and cleared the queue.", ephemeral=True)

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


async def probe_m3u8_duration(url: str) -> float | None:
    """Try to calculate total duration directly from a VOD M3U8 playlist."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/140 Safari/537.36",
        "Accept": "*/*",
    }

    async def fetch_playlist(session, playlist_url: str) -> tuple[str, str] | None:
        try:
            async with session.get(
                playlist_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status != 200:
                    return None
                return await response.text(), str(response.url)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return None

    try:
        async with aiohttp.ClientSession() as session:
            first = await fetch_playlist(session, url)
            if first is None:
                return None
            text, final_url = first

            # Master playlist: use the first variant and inspect that media playlist.
            if "#EXT-X-STREAM-INF" in text:
                variant = None
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        variant = line
                        break
                if variant:
                    variant_url = __import__("urllib.parse", fromlist=["urljoin"]).urljoin(
                        final_url, variant
                    )
                    second = await fetch_playlist(session, variant_url)
                    if second is None:
                        return None
                    text, final_url = second

            values = re.findall(r"#EXTINF:([0-9]+(?:\.[0-9]+)?),", text)
            if not values:
                return None
            total = sum(float(value) for value in values)
            return total if total > 0 else None
    except Exception:
        return None


async def stream_m3u8_movie(url: str, progress=None) -> tuple[bool, str, Path | None]:
    """Download the complete M3U8 movie, show progress, then prepare two-part hosting."""
    global active_host
    ensure_movies_dir()
    if not is_m3u8_url(url):
        return False, "That is not a valid HTTP/HTTPS M3U8 URL.", None
    if progress is not None:
        await progress("Downloading movie... 0%")
    if not PUBLIC_BASE_URL:
        if progress is not None:
            await progress("Connecting movie player...")
        await bot.ensure_cloudflare_tunnel()
    if not PUBLIC_BASE_URL:
        return False, "Movie streaming is unavailable. Make sure cloudflared is installed and in PATH, then restart the bot.", None

    filename = safe_download_name(url)
    output = MOVIES_DIR / filename

    # ffprobe can fail on signed CDN URLs even when FFmpeg can play them.
    # Parse the VOD playlist as a second way to obtain an accurate duration.
    duration = await probe_m3u8_duration(url)

    async with host_lock:
        if active_host is not None:
            return False, "Another movie is currently being hosted. Please wait until it finishes.", None

        token = secrets.token_urlsafe(32)
        HLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        hls_dir = HLS_CACHE_DIR / token
        hls_dir.mkdir(parents=True, exist_ok=False)
        playlist = hls_dir / "playlist.m3u8"

        parsed_url = urlparse(url)
        origin = f"{parsed_url.scheme}://{parsed_url.netloc}/"
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        )
        request_headers = (
            f"User-Agent: {user_agent}\r\n"
            f"Referer: {origin}\r\n"
            "Accept: */*\r\n"
            "Connection: keep-alive\r\n"
        )

        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                "-allowed_extensions", "ALL", "-extension_picky", "0",
                "-user_agent", user_agent,
                "-referer", origin,
                "-headers", request_headers,
                "-http_persistent", "1",
                "-reconnect", "1",
                "-reconnect_at_eof", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-rw_timeout", "30000000",
                "-i", url,
                "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
                "-start_number", "0", "-hls_time", "2", "-hls_list_size", "0",
                "-hls_playlist_type", "vod",
                "-hls_flags", "independent_segments+temp_file",
                "-hls_segment_type", "mpegts", "-f", "hls", str(playlist),
                "-progress", "pipe:1", "-nostats",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            hls_dir.rmdir()
            return False, "FFmpeg is not installed or is not in PATH. Install FFmpeg and restart the bot.", None
        except OSError as exc:
            logger.exception("Could not start FFmpeg for M3U8 download: %s", exc)
            hls_dir.rmdir()
            return False, "I could not start the M3U8 download.", None

        ffmpeg_stderr_lines: list[str] = []
        ffmpeg_stderr_task = asyncio.create_task(_collect_ffmpeg_stderr(process, ffmpeg_stderr_lines))

        async def monitor_download() -> None:
            last_percent = None
            if process.stdout is None:
                return
            try:
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    value = line.decode("utf-8", errors="replace").strip()
                    if not value.startswith("out_time_ms="):
                        continue
                    try:
                        media_time = int(value.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue

                    if duration:
                        percent = max(0, min(99, int((media_time / duration) * 100)))
                        display = f"{percent}%"
                    else:
                        # Signed/odd CDN playlists sometimes hide duration from
                        # ffprobe. Still show real progress instead of 0% forever.
                        display = f"{int(media_time // 60)}m {int(media_time % 60):02d}s"

                    if display != last_percent:
                        last_percent = display
                        if progress is not None:
                            await progress(f"Downloading movie... {display}")
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError):
                pass

        progress_task = asyncio.create_task(monitor_download())
        active_host = {
            "token": token, "movie": output, "hls_dir": hls_dir,
            "ffmpeg_process": process, "ffmpeg_stderr_lines": ffmpeg_stderr_lines,
            "ffmpeg_stderr_task": ffmpeg_stderr_task, "progress_task": progress_task,
            "expires_at": float("inf"), "task": None, "downloading": True,
            "duration": duration,
        }
        active_host["task"] = asyncio.create_task(
            finish_m3u8_host(token, process, playlist, output, progress)
        )

    # Do not wait for the whole movie here. As soon as FFmpeg has created a
    # usable local HLS playlist, return the player URL and let the background
    # task finish the download/remux. This makes playback start while downloading.
    for _ in range(120):
        if playlist.exists() and playlist.stat().st_size > 0:
            if progress is not None:
                await progress("Movie stream ready. You can watch it now.")
            logger.info("M3U8 playback is ready: %s", playlist)
            return True, f"{PUBLIC_BASE_URL}/movie/{token}", output

        if process.returncode is not None:
            error_text = "\n".join(ffmpeg_stderr_lines[-50:])
            logger.error(
                "FFmpeg M3U8 download failed before playback became ready: %s",
                error_text[-5000:] or "(no stderr output)",
            )
            await clear_hosted_movie(token, reason="M3U8 download failed before playback")
            return False, "FFmpeg could not start playback for this M3U8 movie. Check the bot console.", None

        await asyncio.sleep(0.5)

    await clear_hosted_movie(token, reason="M3U8 playback did not start")
    return False, "The M3U8 stream did not become playable in time.", None


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
    process: asyncio.subprocess.Process,
    playlist: Path,
    output: Path,
    progress=None,
) -> None:
    try:
        await process.wait()

        async with host_lock:
            current = active_host
            if current is None or current["token"] != token:
                return
            stderr_task = current.get("ffmpeg_stderr_task")
            stderr_lines = current.get("ffmpeg_stderr_lines", [])

        if stderr_task is not None:
            await stderr_task

        error_text = "\n".join(stderr_lines[-100:])
        if process.returncode != 0 or not playlist.exists() or playlist.stat().st_size == 0:
            logger.error(
                "FFmpeg finished M3U8 download with code %s: %s",
                process.returncode,
                error_text[-5000:] or "(no stderr output)",
            )
            await clear_hosted_movie(token, reason="M3U8 download failed")
            return

        async with host_lock:
            current = active_host
            if current is None or current["token"] != token:
                return
            current["downloading"] = False

        if progress is not None:
            try:
                await progress("Downloading movie... 100%")
                await progress("Preparing movie...")
            except discord.HTTPException:
                pass

        # Convert the cached HLS segments into the normal local MP4 without
        # contacting the original M3U8 URL again.
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
            else:
                remux_temp.replace(output)
        except (FileNotFoundError, OSError) as exc:
            logger.warning("Could not remux completed M3U8 movie: %s", exc)
            remux_temp.unlink(missing_ok=True)

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
            current["expires_at"] = asyncio.get_running_loop().time() + HOST_EXPIRY_BUFFER_SECONDS

        duration = float(current.get("duration", 0) or 0)
        async with host_lock:
            current = active_host
            if current is not None and current["token"] == token:
                current["expires_at"] = asyncio.get_running_loop().time() + duration + HOST_EXPIRY_BUFFER_SECONDS
                current["expiry_task"] = asyncio.create_task(expire_hosted_movie(token, duration))
        if progress is not None:
            try:
                await progress("Movie downloaded and ready.")
            except discord.HTTPException:
                pass
        logger.info("M3U8 movie is fully prepared for playback: %s", output.name)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("M3U8 streaming task failed: %s", exc)
        await clear_hosted_movie(token, reason="M3U8 streaming task failed")


@movie_group.command(name="play", description="Send a local movie or download an M3U8 movie.")
@app_commands.describe(movie="Movie name, or an HTTP/HTTPS .m3u8 URL.")
async def movie_play(interaction: discord.Interaction, movie: str) -> None:
    if is_m3u8_url(movie):
        await interaction.response.defer(ephemeral=True)

        async def progress(message: str) -> None:
            try:
                await interaction.edit_original_response(content=message)
            except discord.HTTPException:
                pass

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

        movie_name = downloaded.stem if downloaded is not None else "M3U8 Movie"
        try:
            # Keep the Discord message simple for now. The player/embed
            # presentation can be improved separately later.
            # M3U8 playback starts immediately through the HLS player. Once
            # the download is complete, the same URL exposes the direct MP4
            # preview through its Open Graph metadata.
            await channel.send(
                content=f"▶ **Now Playing:** {movie_name}\n{player_url}"
            )
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

    try:
        # Plain URL for now. Discord's native media preview can be revisited
        # later without changing the hosting/player architecture.
        await channel.send(
            content=f"▶ **Now Playing:** {selected.stem}\n{message}"
        )
        await interaction.followup.send(f"Now hosting {selected.stem} in {channel.mention}.", ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        await clear_hosted_movie(active_host["token"] if active_host else "")
        await interaction.followup.send("I could not post the movie player in the configured channel.", ephemeral=True)


@bot.tree.command(name="sync", description="Sync all slash commands to this server.")
@app_commands.checks.has_permissions(administrator=True)
async def sync_commands(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        bot.tree.copy_global_to(guild=interaction.guild)
        synced = await bot.tree.sync(guild=interaction.guild)
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.warning("Could not sync commands to guild %s: %s", interaction.guild.id, exc)
        await interaction.followup.send(
            "I could not sync the commands. Check that I have permission to use slash commands in this server.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"Synced {len(synced)} slash command(s) to **{interaction.guild.name}**. "
        "They should appear immediately.",
        ephemeral=True,
    )


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
