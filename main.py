import asyncio
import sys
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse
from html import escape

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
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

load_dotenv()


BASE_DIR = Path(__file__).resolve().parent
MOVIES_DIR = BASE_DIR / "Movies"
CONFIG_FILE = BASE_DIR / "config.json"
SUPPORTED_EXTENSIONS = {".mp4", ".mp3"}
M3U8_SUFFIX = ".m3u8"
# Allow hosts to provide system-installed media tools at nonstandard paths.
# These executables are deliberately not downloaded by the bot at startup.
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg"
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe").strip() or "ffprobe"
DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
HOST_EXPIRY_BUFFER_SECONDS = 30
WEB_HOST = os.getenv("MOVIE_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("MOVIE_PORT", "8080"))
HLS_CACHE_DIR = BASE_DIR / ".movie_hls"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://wisp.uno").strip().rstrip("/")
# Downloaded VidNest/M3U8/Cineby movies are deleted after this long to keep
# the 512 MB disk quota from filling up. Hosting works from the HLS cache
# while a movie plays, so deleting later is safe.
DOWNLOAD_LIFETIME_SECONDS = 24 * 3600
# Browser identity used when downloading streams; many CDNs reject ffmpeg's
# default User-Agent.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
)
CINEBY_REFERER = "https://cineby.tech/"

# TMDB provides movie search results; VidNest accepts TMDB IDs in its player URL.
TMDB_API_KEY = os.getenv(
    "TMDB_API_KEY",
    os.getenv("CINEBY_TMDB_API_KEY", "8871b4dba1715cd776c063a458ae8795"),
).strip()
TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
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
            FFPROBE_BIN, "-v", "error",
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

    if shutil.which(FFPROBE_BIN) is None:
        return False, (
            "ffprobe is unavailable. Install FFmpeg (including ffprobe) on the host, "
            "or set FFPROBE_BIN to the ffprobe executable path, then restart the bot."
        )
    if shutil.which(FFMPEG_BIN) is None:
        return False, (
            "FFmpeg is unavailable. Install FFmpeg on the host, or set FFMPEG_BIN "
            "to the ffmpeg executable path, then restart the bot."
        )

    duration = await get_media_duration(resolved)
    if duration is None:
        return False, "ffprobe could not read this file or determine a valid duration."

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
                FFMPEG_BIN,
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


def is_m3u8_url(value: str) -> bool:
    """Return True for a valid HTTP(S) URL whose path is an M3U8 playlist."""
    try:
        parsed = urlparse(value.strip())
    except (AttributeError, ValueError):
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.path.lower().endswith(M3U8_SUFFIX)
    )


async def stream_m3u8_movie(
    url: str,
    progress=None,
    referer: str | None = None,
) -> tuple[bool, str | None, Path | None]:
    """Download an M3U8 stream to Movies, then prepare the normal hosted player."""
    if not is_m3u8_url(url):
        return False, None, None

    if shutil.which(FFMPEG_BIN) is None:
        logger.error("Cannot process M3U8 input: FFmpeg executable %r was not found.", FFMPEG_BIN)
        return False, None, None
    if shutil.which(FFPROBE_BIN) is None:
        logger.error("Cannot validate M3U8 output: ffprobe executable %r was not found.", FFPROBE_BIN)
        return False, None, None

    ensure_movies_dir()
    filename = f"M3U8-{secrets.token_hex(5)}.mp4"
    downloaded = MOVIES_DIR / filename
    temporary = MOVIES_DIR / f".{filename}.part.mp4"

    if progress is not None:
        await progress("Downloading the M3U8 movie...")

    download_command = [
        FFMPEG_BIN,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        # Present as a browser; many stream CDNs reject ffmpeg's default UA.
        "-user_agent",
        BROWSER_USER_AGENT,
        # Some HLS playlists include signed CDN segment URLs without a
        # conventional media extension (including image/ad placeholders).
        # Permit those URLs so FFmpeg can inspect the playlist and continue
        # past entries that are not usable media segments.
        "-allowed_extensions",
        "ALL",
    ]
    if referer:
        # Some providers require an origin/referer that matches the page
        # the stream was discovered on.
        download_command += ["-headers", f"Referer: {referer}\r\n"]
    download_command += [
        "-i",
        url.strip(),
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temporary),
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *download_command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, None, None
    except OSError as exc:
        logger.warning("Could not start M3U8 download: %s", exc)
        return False, None, None

    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        temporary.unlink(missing_ok=True)
        logger.warning("M3U8 download exceeded the %s-second timeout.", DOWNLOAD_TIMEOUT_SECONDS)
        return False, None, None
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        temporary.unlink(missing_ok=True)
        raise

    if process.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
        error_text = stderr.decode("utf-8", errors="replace").strip()
        logger.warning("M3U8 download failed: %s", error_text[-2000:])
        temporary.unlink(missing_ok=True)
        return False, None, None

    duration = await get_media_duration(temporary)
    if duration is None:
        logger.warning("Downloaded M3U8 output is not a readable media file.")
        temporary.unlink(missing_ok=True)
        return False, None, None

    try:
        temporary.replace(downloaded)
    except OSError as exc:
        logger.warning("Could not finalize downloaded M3U8 movie: %s", exc)
        temporary.unlink(missing_ok=True)
        return False, None, None

    if progress is not None:
        await progress("Preparing the Discord movie player...")

    success, player_url = await host_movie(downloaded, progress)
    if not success:
        return False, None, downloaded

    return True, player_url, downloaded


async def _play_m3u8_movie(
    interaction: discord.Interaction,
    m3u8_url: str,
    referer: str | None = None,
) -> None:
    """Download an M3U8 playlist and announce the hosted player in the movie channel.

    Shared by direct .m3u8 links, resolved VidNest pages, and resolved Cineby
    pages. Assumes the interaction has already been acknowledged/deferred.
    """
    async def progress(message: str) -> None:
        try:
            await interaction.edit_original_response(content=message)
        except discord.HTTPException:
            pass

    success, player_url, downloaded = await stream_m3u8_movie(m3u8_url, progress, referer=referer)

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


@tasks.loop(hours=6)
async def prune_movie_downloads() -> None:
    """Disk-safety task: delete aged M3U8/Cineby downloads and stale caches.

    Runs once at startup, then every six hours. Skips the currently hosted
    movie so an in-progress hosting session is never deleted out from under
    the player.
    """
    ensure_movies_dir()
    cutoff = time.time() - DOWNLOAD_LIFETIME_SECONDS

    hosted_paths: list[Path] = []
    if active_host is not None:
        try:
            hosted_paths.append(active_host["movie"].resolve())
        except OSError:
            pass

    for pattern in ("M3U8-*.mp4", "M3U8-*.mp3"):
        for path in MOVIES_DIR.glob(pattern):
            try:
                if path.resolve() in hosted_paths:
                    continue
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    logger.info("Pruned old downloaded movie: %s", path.name)
            except OSError:
                continue

    # Remove abandoned partial downloads (crash leftovers).
    for path in MOVIES_DIR.glob(".*.part.mp4"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                logger.info("Pruned partial download: %s", path.name)
        except OSError:
            continue

    # Remove stale HLS cache folders that no active host is using.
    if HLS_CACHE_DIR.is_dir():
        active_hls: list[Path] = []
        if active_host is not None:
            try:
                active_hls.append(active_host["hls_dir"].resolve())
            except OSError:
                pass
        for hls_dir in list(HLS_CACHE_DIR.iterdir()):
            try:
                if not hls_dir.is_dir() or hls_dir.resolve() in active_hls:
                    continue
                if hls_dir.stat().st_mtime < cutoff:
                    for entry in hls_dir.rglob("*"):
                        if entry.is_file():
                            entry.unlink(missing_ok=True)
                    hls_dir.rmdir()
                    logger.info("Pruned stale HLS cache: %s", hls_dir.name)
            except OSError:
                continue


class VidNestSearchView(discord.ui.View):
    """A private title picker for movie results."""

    def __init__(self, owner_id: int, results: list[dict]):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.results_by_id = {str(item["id"]): item for item in results}
        options = []
        for item in results[:25]:
            title = str(item.get("title") or "Untitled movie")
            year = str(item.get("release_date") or "")[:4]
            label = f"{title} ({year})" if year else title
            overview = " ".join(str(item.get("overview") or "").split())
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    description=(overview or "No description available.")[:100],
                    value=str(item["id"]),
                )
            )

        self.movie_select = discord.ui.Select(
            placeholder="Choose a movie...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.movie_select.callback = self.movie_selected
        self.add_item(self.movie_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_response(
            interaction,
            "This movie selector belongs to the person who ran /movie search.",
            ephemeral=True,
        )
        return False

    async def movie_selected(self, interaction: discord.Interaction) -> None:
        item = self.results_by_id.get(self.movie_select.values[0])
        if item is None:
            await send_interaction_response(
                interaction,
                "I couldn't find that selection. Run /movie search again.",
                ephemeral=True,
            )
            return

        title = str(item.get("title") or "Untitled movie")
        release_date = str(item.get("release_date") or "")
        year = release_date[:4]
        movie_id = int(item["id"])
        overview = str(item.get("overview") or "No description is available.")
        poster_path = str(item.get("poster_path") or "")
        vidnest_url = f"https://vidnest.fun/movie/{movie_id}"
        cineby_url = f"https://cineby.tech/movie/{movie_id}/watch"

        embed = discord.Embed(
            title=f"{title} ({year})" if year else title,
            description=overview[:4000],
            url=vidnest_url,
            color=discord.Color.blurple(),
        )
        if poster_path.startswith("/"):
            embed.set_thumbnail(url=f"https://image.tmdb.org/t/p/w500{poster_path}")
        embed.add_field(
            name="Watch option 1 — VidNest",
            value=f"[Open this title on VidNest]({vidnest_url})",
            inline=False,
        )
        embed.add_field(
            name="Watch option 2 — Cineby",
            value=f"[Open this title on Cineby]({cineby_url})",
            inline=False,
        )
        await interaction.response.edit_message(
            content=f"Selected **{title}**" + (f" ({year})" if year else "") + ". Choose either provider below.",
            embed=embed,
            view=MoviePlaybackView(self.owner_id, vidnest_url, cineby_url),
        )
        self.stop()


class MoviePlaybackView(discord.ui.View):
    """Offers provider page links only; it does not inspect or extract media."""

    def __init__(self, owner_id: int, vidnest_url: str, cineby_url: str):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.add_item(
            discord.ui.Button(
                label="Watch on VidNest",
                style=discord.ButtonStyle.link,
                url=vidnest_url,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Watch on Cineby",
                style=discord.ButtonStyle.link,
                url=cineby_url,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_response(
            interaction,
            "This movie selection belongs to the person who ran /movie search.",
            ephemeral=True,
        )
        return False


async def search_movies(query: str) -> list[dict]:
    """Search TMDB; the results use TMDB movie IDs."""
    if not TMDB_API_KEY:
        raise RuntimeError("TMDB_API_KEY is not configured.")

    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"User-Agent": "Movie-Bot/1.0 (+https://vidnest.fun)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(
            TMDB_SEARCH_URL,
            params={
                "api_key": TMDB_API_KEY,
                "query": query,
                "include_adult": "false",
                "page": "1",
            },
        ) as response:
            if response.status == 401:
                raise RuntimeError("The movie catalog API key was rejected.")
            response.raise_for_status()
            data = await response.json(content_type=None)

    results = data.get("results", []) if isinstance(data, dict) else []
    return [
        item for item in results
        if isinstance(item, dict) and isinstance(item.get("id"), int)
    ][:25]


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


@movie_group.command(name="search", description="Search for a movie title.")
@app_commands.describe(title="The movie title to search for.")
@app_commands.guild_only()
async def movie_search(interaction: discord.Interaction, title: str) -> None:
    await acknowledge_command(interaction)
    query = " ".join(title.split()).strip()
    if len(query) < 2:
        await send_interaction_response(
            interaction,
            "Enter at least two characters to search for a movie.",
            ephemeral=True,
        )
        return
    if len(query) > 100:
        await send_interaction_response(
            interaction,
            "Movie searches must be 100 characters or fewer.",
            ephemeral=True,
        )
        return

    try:
        results = await search_movies(query)
    except asyncio.TimeoutError:
        await send_interaction_response(
            interaction,
            "The movie catalog search timed out. Please try again.",
            ephemeral=True,
        )
        return
    except (aiohttp.ClientError, ValueError, RuntimeError) as exc:
        logger.warning("TMDB movie search failed for %r: %s", query, exc)
        await send_interaction_response(
            interaction,
            "I couldn't search the movie catalog right now. Please try again later.",
            ephemeral=True,
        )
        return

    if not results:
        await send_interaction_response(
            interaction,
            f"No movies found for **{escape(query)}**.",
            ephemeral=True,
        )
        return

    view = VidNestSearchView(interaction.user.id, results)
    await send_interaction_response(
        interaction,
        content=f"Search results for **{escape(query)}** — choose a movie below.",
        view=view,
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



@movie_group.command(
    name="play",
    description="Post a movie link without downloading or extracting media.",
)
@app_commands.describe(
    movie="A direct movie page URL or a title to look up with /movie search.",
)
async def movie_play(interaction: discord.Interaction, movie: str) -> None:
    """Post a user-provided movie page URL. Never scrape, extract, or download media."""
    await acknowledge_command(interaction)

    value = movie.strip()
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        movie_url = value
    else:
        await interaction.followup.send(
            "I don't search or extract movie streams. Use **/movie search** to find a title "
            "and open its listed provider page, then use **/movie play** with that page URL "
            "to share the link here.",
            ephemeral=True,
        )
        return

    channel = await get_movie_channel()
    if channel is None:
        await interaction.followup.send(
            "The configured movie channel is unavailable. Check the movie channel configuration.",
            ephemeral=True,
        )
        return

    try:
        await channel.send(
            f"▶ **Movie link**\n{movie_url}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(
            "I couldn't post the movie link in the configured channel.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"Posted the movie link in {channel.mention}. No media was downloaded.",
        ephemeral=True,
    )



LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"

# Lavalink runs separately. Set LAVALINK_URI and LAVALINK_PASSWORD in the
# hosting environment to match the external Lavalink server.
music_states: dict[int, dict] = {}
lavalink_ready = False


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
    """Resolve a Spotify link or search query using the configured Lavalink node."""
    if not lavalink_ready or wavelink is None:
        return None

    is_spotify = spotify_track_url(query)
    spotify_info = await resolve_spotify_track(query) if is_spotify else None
    if is_spotify and spotify_info is None:
        logger.warning("Could not resolve Spotify metadata for %r.", query)
        return None

    search_term = (
        f"{spotify_info['artist']} - {spotify_info['title']}"
        if spotify_info
        else query.strip()
    )
    if not search_term:
        return None

    search_errors: list[str] = []

    # Music resolution stays on Lavalink. Do not invoke yt-dlp or attempt
    # direct YouTube extraction in the bot container.
    for source_query in (f"ytsearch:{search_term}", f"scsearch:{search_term}"):
        try:
            results = await wavelink.Pool.fetch_tracks(source_query)
            tracks = list(results.tracks) if isinstance(results, wavelink.Playlist) else list(results or [])
            if tracks:
                playable = tracks[0]
                title = str(getattr(playable, "title", "") or search_term).strip()
                artist = (
                    str(spotify_info["artist"]).strip()
                    if spotify_info
                    else str(getattr(playable, "author", "") or "Unknown Artist").strip()
                )
                return {
                    "track": playable,
                    "title": title,
                    "artist": artist or "Unknown Artist",
                    "duration": max(0, int(getattr(playable, "length", 0) or 0)),
                    "spotify_url": spotify_info.get("spotify_url") if spotify_info else None,
                    "youtube_url": getattr(playable, "uri", None),
                    "lyrics": [],
                }
        except Exception as exc:
            search_errors.append(f"{source_query.split(':', 1)[0]}: {exc}")
            logger.warning("Lavalink search failed for %r: %s", source_query, exc)

    if search_errors:
        logger.warning("No playable source found for %r. Resolution errors: %s", search_term, "; ".join(search_errors))
    return None

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


async def connect_member_voice(interaction: discord.Interaction, member_override=None):
    if interaction.guild is None or wavelink is None:
        return None

    member = member_override if member_override is not None else interaction.user
    if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
        return None

    voice_channel = member.voice.channel
    state = get_music_state(interaction.guild.id)

    # Check permissions before beginning the voice handshake, and log the
    # exact missing permissions for easier hosting/server troubleshooting.
    me = interaction.guild.me
    if me is not None:
        permissions = voice_channel.permissions_for(me)
        missing = [
            name for name, allowed in (
                ("View Channel", permissions.view_channel),
                ("Connect", permissions.connect),
                ("Speak", permissions.speak),
            ) if not allowed
        ]
        if missing:
            logger.warning(
                "Missing voice permissions in guild %s channel %s: %s",
                interaction.guild.id, voice_channel.id, ", ".join(missing),
            )
            return None

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

            logger.exception(
                "Voice connection failed for guild %s, channel %s (%s); "
                "exception_type=%s",
                interaction.guild.id,
                voice_channel.id,
                voice_channel.name,
                type(exc).__name__,
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


# Create the Discord client before registering slash commands and events.
intents = discord.Intents.default()
intents.message_content = True


class MovieBot(commands.Bot):
    async def setup_hook(self) -> None:
        # Connect to the configured external Lavalink node and publish slash commands.
        await initialize_lavalink()
        try:
            synced = await self.tree.sync()
            logger.info("Synced %d global slash command(s).", len(synced))
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Could not sync global slash commands on startup: %s", exc)


bot = MovieBot(command_prefix=commands.when_mentioned, intents=intents)


@bot.tree.command(name="play", description="Play a song or Spotify track in your voice channel.")
@app_commands.describe(song="A song name, or an open.spotify.com/track URL.")
async def music_play(interaction: discord.Interaction, song: str) -> None:
    await acknowledge_command(interaction)
    await interaction.followup.send("The music player is in beta.", ephemeral=True)
    if interaction.guild is None:
        await send_interaction_response(interaction, "This command only works in a server.", ephemeral=True)
        return

    if not lavalink_ready:
        await interaction.edit_original_response(content="The music backend is currently unavailable.")
        return

    player = await connect_member_voice(interaction)
    if player is None:
        member = interaction.user
        if isinstance(member, discord.Member) and member.voice and member.voice.channel:
            await interaction.edit_original_response(
                content="I couldn't join that voice channel. Check that I have View Channel, Connect, and Speak permissions, and that the channel isn't full."
            )
        else:
            await interaction.edit_original_response(content="Join a voice channel first. I can then play the song there.")
        return

    try:
        track = await resolve_music_source(song)
    except Exception as exc:
        logger.exception("Music source resolution failed: %s", exc)
        track = None

    if track is None:
        await interaction.edit_original_response(content="I couldn't find or load that song from the music backend.")
        return

    state = get_music_state(interaction.guild.id)
    was_playing = state.get("current") is not None or bool(player.playing) or bool(state.get("queue"))
    state["queue"].append(track)
    position = len(state["queue"])
    started = await play_next(interaction.guild.id)

    if not was_playing and not started and state.get("current") is None:
        await interaction.edit_original_response(
            content="I couldn't load that audio source from the music backend."
        )
        return

    if was_playing:
        message = f"Queued **{track['title']}** by **{track['artist']}** at position {position}."
    else:
        message = f"Playing **{track['title']}** by **{track['artist']}**."
    await interaction.edit_original_response(content=message)


@bot.tree.command(name="pause", description="Pause the current song.")
async def music_pause(interaction: discord.Interaction) -> None:
    await acknowledge_command(interaction)
    if interaction.guild is None:
        await send_interaction_response(interaction, "This command only works in a server.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    player = state.get("player")
    if player is None or not player.playing or player.paused:
        await send_interaction_response(interaction, "Nothing is currently playing.", ephemeral=True)
        return
    try:
        await player.pause(True)
    except Exception as exc:
        logger.warning("Could not pause music: %s", exc)
        await send_interaction_response(interaction, "I couldn't pause the current song.", ephemeral=True)
        return
    await send_interaction_response(interaction, "Paused the current song.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the paused song.")
async def music_resume(interaction: discord.Interaction) -> None:
    await acknowledge_command(interaction)
    if interaction.guild is None:
        await send_interaction_response(interaction, "This command only works in a server.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    player = state.get("player")
    if player is None or not player.paused:
        await send_interaction_response(interaction, "The song is not paused.", ephemeral=True)
        return
    try:
        await player.pause(False)
    except Exception as exc:
        logger.warning("Could not resume music: %s", exc)
        await send_interaction_response(interaction, "I couldn't resume the current song.", ephemeral=True)
        return
    await send_interaction_response(interaction, "Resumed the current song.", ephemeral=True)


@bot.tree.command(name="lyrics", description="Toggle synchronized lyrics on or off.")
@app_commands.describe(enabled="Turn synchronized lyrics on or off. Leave empty to toggle.")
async def music_lyrics(interaction: discord.Interaction, enabled: bool | None = None) -> None:
    await acknowledge_command(interaction)
    if interaction.guild is None:
        await send_interaction_response(interaction, "This command only works in a server.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    if enabled is None:
        enabled = not state.get("lyrics_enabled", False)
    state["lyrics_enabled"] = bool(enabled)
    if state["lyrics_enabled"]:
        await send_interaction_response(
            interaction,
            "Warning: This Feature Is In Beta, Don't Expect A Fully Working Version Soon",
            ephemeral=True,
        )
    if state["lyrics_enabled"] and state.get("current") is not None:
        await cancel_lyrics(interaction.guild.id)
        player = state.get("player")
        current = state.get("current")
        if player is not None and current is not None and current.get("lyrics"):
            state["lyrics_task"] = asyncio.create_task(
                lyrics_loop(
                    interaction.guild.id,
                    player,
                    player.channel,
                    current,
                    start_from_position=True,
                )
            )
    status = "enabled" if enabled else "disabled"
    await send_interaction_response(interaction, f"Synchronized lyrics are now **{status}**.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip the currently playing song.")
async def music_skip(interaction: discord.Interaction) -> None:
    await acknowledge_command(interaction)
    if interaction.guild is None:
        await send_interaction_response(interaction, "This command only works in a server.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    player = state.get("player")
    if player is None or not player.playing:
        await send_interaction_response(interaction, "Nothing is currently playing.", ephemeral=True)
        return
    await cancel_lyrics(interaction.guild.id)
    try:
        await player.stop()
    except Exception as exc:
        logger.warning("Could not skip current track: %s", exc)
        await send_interaction_response(interaction, "I couldn't skip the current song.", ephemeral=True)
        return
    await send_interaction_response(interaction, "Skipped the current song.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the current music queue.")
async def music_queue(interaction: discord.Interaction) -> None:
    await acknowledge_command(interaction)
    if interaction.guild is None:
        await send_interaction_response(interaction, "This command only works in a server.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    current = state.get("current")
    queue = state.get("queue", [])
    lines: list[str] = []
    if current is not None:
        seconds = current.get("duration", 0) / 1000
        lines.append(f"**Now playing:** {current['title']} — {current['artist']} ({int(seconds // 60)}:{int(seconds % 60):02d})")
    for index, track in enumerate(queue, 1):
        seconds = track.get("duration", 0) / 1000
        lines.append(f"**{index}.** {track['title']} — {track['artist']} ({int(seconds // 60)}:{int(seconds % 60):02d})")
    if not lines:
        lines = ["The music queue is empty."]
    await send_interaction_response(interaction, "\n".join(lines[:51]), ephemeral=True)


@bot.tree.command(name="volume", description="Set the music volume.")
@app_commands.describe(level="Volume from 0 to 100.")
async def music_volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]) -> None:
    await acknowledge_command(interaction)
    if interaction.guild is None:
        await send_interaction_response(interaction, "This command only works in a server.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    state["volume"] = int(level)
    player = state.get("player")
    if player is not None and player.connected:
        try:
            await player.set_volume(int(level))
        except Exception as exc:
            logger.warning("Could not change Lavalink volume: %s", exc)
            await send_interaction_response(interaction, "I couldn't change the current volume.", ephemeral=True)
            return
    await send_interaction_response(interaction, f"Volume set to **{level}%**.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop music and clear the queue.")
async def music_stop(interaction: discord.Interaction) -> None:
    await acknowledge_command(interaction)
    if interaction.guild is None:
        await send_interaction_response(interaction, "This command only works in a server.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    state["queue"].clear()
    await cancel_lyrics(interaction.guild.id)
    player = state.get("player")
    if player is not None and player.playing:
        state["stopping"] = True
        try:
            await player.stop()
        except Exception as exc:
            logger.warning("Could not stop Lavalink playback: %s", exc)
        finally:
            state["stopping"] = False
    state["current"] = None
    await send_interaction_response(interaction, "Stopped playback and cleared the queue.", ephemeral=True)


BOT_START_TIME = time.monotonic()
GITHUB_UPDATES_RAW_URL = "https://raw.githubusercontent.com/WebsiteDevelopmentStudios/Movie-Bot/main/updates.txt"


def format_uptime() -> str:
    total = max(0, int(time.monotonic() - BOT_START_TIME))
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, seconds = divmod(total, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def split_discord_message(text: str, limit: int = 1900) -> list[str]:
    """Split long content into Discord-safe chunks, preferring line boundaries."""
    chunks = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks or ["The update log is empty."]


async def fetch_updates() -> str:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(GITHUB_UPDATES_RAW_URL) as response:
            response.raise_for_status()
            return await response.text()


async def post_updates(channel) -> int:
    updates = await fetch_updates()
    chunks = split_discord_message(updates)
    for index, chunk in enumerate(chunks, 1):
        await channel.send(
            chunk,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    return len(chunks)


@bot.event
async def on_message(message: discord.Message) -> None:
    # Text command prefix is intentionally "-mb"; slash commands remain enabled.
    if message.author.bot or message.guild is None:
        return
    if not message.content.lower().startswith("-mb"):
        return

    raw = message.content[3:].strip()
    if not raw:
        await message.reply("Run `-mb commands` to see the available commands.", mention_author=False)
        return

    parts = raw.split(maxsplit=1)
    command_name = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""
    guild_id = message.guild.id
    state = get_music_state(guild_id)
    player = state.get("player")

    try:
        if command_name == "sync":
            if not message.author.guild_permissions.administrator:
                await message.reply("You need Administrator permission to use this command.", mention_author=False)
                return
            try:
                bot.tree.clear_commands(guild=message.guild)
                await bot.tree.sync(guild=message.guild)
                synced = await bot.tree.sync()
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Could not reset/sync slash commands from prefix command: %s", exc)
                await message.reply(
                    "I could not reset and sync the commands. Check the bot's application-command permissions and try again.",
                    mention_author=False,
                )
                return
            logger.info(
                "Cleared guild-specific commands and synced %d global slash command(s) for %s (%s) via -mb sync.",
                len(synced),
                message.guild.name,
                message.guild.id,
            )
            await message.reply(
                f"Removed duplicate server-specific commands and synced {len(synced)} global slash command(s). They may take a little while to refresh.",
                mention_author=False,
            )
            return

        if command_name == "uptime":
            await message.reply(f"I have been online for **{format_uptime()}**.", mention_author=False)
            return

        if command_name == "update":
            await message.reply("Fetching the latest update log and posting it here...", mention_author=False)
            try:
                count = await post_updates(message.channel)
                await message.reply(f"Posted the update log in {count} message(s).", mention_author=False)
            except Exception as exc:
                logger.warning("Could not post update log: %s", exc)
                await message.reply("I couldn't fetch the update log. Please try again later.", mention_author=False)
            return

        if command_name == "say":
            if not message.author.guild_permissions.manage_messages:
                await message.reply("You need Manage Messages permission to use this command.", mention_author=False)
                return
            if not argument:
                await message.reply("Usage: `-mb say <message>`", mention_author=False)
                return
            await message.channel.send(
                argument,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if command_name in {"help", "commands"}:
            await message.reply(
                "**Movie Bot text commands**\n"
                "`-mb play <song>` — play or queue a song\n"
                "`-mb pause` / `-mb resume`\n"
                "`-mb skip` / `-mb stop`\n"
                "`-mb queue`\n"
                "`-mb volume <0-100>`\n"
                "`-mb lyrics [on|off]`\n"
                "`-mb join` / `-mb leave`\n"
                "`-mb uptime` / `-mb update` / `-mb commands`\n"
                "`-mb say <message>` (requires Manage Messages)\n"
                "`-mb sync` (requires Administrator)\n\n"
                "**Movie text commands**\n"
                "`-mb movie list` — list available local movies\n"
                "`-mb movie search <title>` — search movie titles\n"
                "`-mb movie play <URL>` — post a movie page link\n"
                "`-mb channel link <#channel or ID>` — set the movie channel (Administrator)\n"
                "**Slash equivalents:** `/movie list`, `/movie search`, `/movie play`, `/channel link`\n",
                mention_author=False,
            )
            return

        if command_name == "movie":
            movie_parts = argument.split(maxsplit=1)
            movie_action = movie_parts[0].lower() if movie_parts else ""
            movie_argument = movie_parts[1].strip() if len(movie_parts) > 1 else ""

            if movie_action == "list":
                movies = get_movie_files()
                if not movies:
                    await message.reply("No local movies are currently available.", mention_author=False)
                    return
                lines = [f"**Available local movies ({len(movies)}):**"]
                lines.extend(f"• {path.stem}" for path in movies)
                output = "\\n".join(lines)
                for start in range(0, len(output), 1900):
                    await message.channel.send(
                        output[start:start + 1900],
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                return

            if movie_action == "search":
                query = " ".join(movie_argument.split()).strip()
                if len(query) < 2:
                    await message.reply("Usage: `-mb movie search <title>`", mention_author=False)
                    return
                if len(query) > 100:
                    await message.reply("Movie searches must be 100 characters or fewer.", mention_author=False)
                    return
                try:
                    results = await search_movies(query)
                except (asyncio.TimeoutError, aiohttp.ClientError, ValueError, RuntimeError) as exc:
                    logger.warning("Text-command movie search failed for %r: %s", query, exc)
                    await message.reply("I couldn't search the movie catalog right now. Please try again later.", mention_author=False)
                    return
                if not results:
                    await message.reply(f"No movies found for **{escape(query)}**.", mention_author=False)
                    return

                result_lines = [f"**Movie search results for {escape(query)}:**"]
                for item in results[:10]:
                    title = str(item.get("title") or "Untitled movie")
                    year = str(item.get("release_date") or "")[:4]
                    movie_id = int(item["id"])
                    suffix = f" ({year})" if year else ""
                    result_lines.append(
                        f"• **{escape(title)}{suffix}** — "
                        f"[VidNest](https://vidnest.fun/movie/{movie_id}) | "
                        f"[Cineby](https://cineby.tech/movie/{movie_id}/watch)"
                    )
                result_lines.append("Use `-mb movie play <URL>` to post a movie page link in the configured movie channel.")
                output = "\\n".join(result_lines)
                if len(output) <= 1900:
                    await message.reply(output, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                else:
                    await message.reply(output[:1900], mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                return

            if movie_action == "play":
                if not movie_argument:
                    await message.reply("Usage: `-mb movie play <URL>`", mention_author=False)
                    return
                parsed_movie_url = urlparse(movie_argument)
                if parsed_movie_url.scheme not in ("http", "https") or not parsed_movie_url.netloc:
                    await message.reply(
                        "Provide a movie page URL. Use `-mb movie search <title>` to find a title first.",
                        mention_author=False,
                    )
                    return
                movie_channel = await get_movie_channel()
                if movie_channel is None:
                    await message.reply("The configured movie channel is unavailable. An administrator can set it with `-mb channel link <#channel or ID>`.", mention_author=False)
                    return
                try:
                    await movie_channel.send(
                        f"▶ **Movie link**\\n{movie_argument}",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    await message.reply("I couldn't post the movie link in the configured channel.", mention_author=False)
                    return
                await message.reply(f"Posted the movie link in {movie_channel.mention}. No media was downloaded.", mention_author=False)
                return

            await message.reply(
                "Usage: `-mb movie list`, `-mb movie search <title>`, or `-mb movie play <URL>`.",
                mention_author=False,
            )
            return

        if command_name == "channel":
            channel_parts = argument.split(maxsplit=1)
            channel_action = channel_parts[0].lower() if channel_parts else ""
            channel_value = channel_parts[1].strip() if len(channel_parts) > 1 else ""
            if channel_action != "link":
                await message.reply("Usage: `-mb channel link <#channel or ID>`", mention_author=False)
                return
            if not message.author.guild_permissions.administrator:
                await message.reply("You need Administrator permission to link the movie channel.", mention_author=False)
                return
            if not channel_value:
                await message.reply("Usage: `-mb channel link <#channel or ID>`", mention_author=False)
                return
            channel_id_match = re.fullmatch(r"<#(\\d+)>|(\\d+)", channel_value)
            if not channel_id_match:
                await message.reply("Mention a text channel or provide its numeric channel ID.", mention_author=False)
                return
            channel_id = int(channel_id_match.group(1) or channel_id_match.group(2))
            linked_channel = message.guild.get_channel(channel_id)
            if not isinstance(linked_channel, discord.TextChannel):
                await message.reply("I couldn't find that text channel in this server.", mention_author=False)
                return
            config["channel_id"] = linked_channel.id
            try:
                save_config(config)
            except OSError as exc:
                logger.error("Could not save movie channel configuration: %s", exc)
                await message.reply("I couldn't save the movie channel configuration.", mention_author=False)
                return
            await message.reply(f"Movie channel linked to {linked_channel.mention}.", mention_author=False)
            return

        if command_name == "play":
            await message.reply("The music player is in beta.", mention_author=False)
            if not argument:
                await message.reply("Usage: `-mb play <song>`", mention_author=False)
                return
            if not lavalink_ready:
                await message.reply("The music backend is currently unavailable.", mention_author=False)
                return
            player = await connect_member_voice(message, member_override=message.author)
            if player is None:
                await message.reply("Join a voice channel first, and make sure I have permission to connect and speak.", mention_author=False)
                return
            track = await resolve_music_source(argument)
            if track is None:
                await message.reply("I couldn't find that song. Try another search.", mention_author=False)
                return
            state = get_music_state(guild_id)
            if player.playing or state.get("current") is not None:
                state["queue"].append(track)
                await message.reply(f"Added **{track['title']} — {track['artist']}** to the queue.", mention_author=False)
            else:
                state["queue"].append(track)
                started = await play_next(guild_id)
                if started:
                    await message.reply(f"Now playing **{track['title']} — {track['artist']}**.", mention_author=False)
                else:
                    await message.reply("I found the song, but couldn't start playback.", mention_author=False)
            return

        if command_name == "join":
            if not lavalink_ready:
                await message.reply("The music backend is currently unavailable.", mention_author=False)
                return
            player = await connect_member_voice(message, member_override=message.author)
            if player is None:
                await message.reply("Join a voice channel first, and make sure I have permission to connect and speak.", mention_author=False)
            else:
                await message.reply(f"Joined **{player.channel.name}**.", mention_author=False)
            return

        if command_name == "leave":
            if player is None or not player.connected:
                await message.reply("I am not in a voice channel.", mention_author=False)
                return
            state["stopping"] = True
            state["queue"].clear()
            await cancel_lyrics(guild_id)
            try:
                await player.disconnect()
            finally:
                state["player"] = None
                state["current"] = None
                state["stopping"] = False
            await message.reply("Left the voice channel and cleared the music queue.", mention_author=False)
            return

        if command_name == "pause":
            if player is None or not player.playing or player.paused:
                await message.reply("Nothing is currently playing.", mention_author=False)
                return
            await player.pause(True)
            await message.reply("Paused the current song.", mention_author=False)
            return

        if command_name == "resume":
            if player is None or not player.paused:
                await message.reply("The song is not paused.", mention_author=False)
                return
            await player.pause(False)
            await message.reply("Resumed the current song.", mention_author=False)
            return

        if command_name == "skip":
            if player is None or not player.playing:
                await message.reply("Nothing is currently playing.", mention_author=False)
                return
            await cancel_lyrics(guild_id)
            await player.stop()
            await message.reply("Skipped the current song.", mention_author=False)
            return

        if command_name == "queue":
            current = state.get("current")
            lines = []
            if current is not None:
                seconds = current.get("duration", 0) / 1000
                lines.append(f"**Now playing:** {current['title']} — {current['artist']} ({int(seconds // 60)}:{int(seconds % 60):02d})")
            for index, queued in enumerate(state.get("queue", []), 1):
                seconds = queued.get("duration", 0) / 1000
                lines.append(f"**{index}.** {queued['title']} — {queued['artist']} ({int(seconds // 60)}:{int(seconds % 60):02d})")
            await message.reply("\\n".join(lines[:51]) if lines else "The music queue is empty.", mention_author=False)
            return

        if command_name == "volume":
            if not argument.isdigit() or not 0 <= int(argument) <= 100:
                await message.reply("Usage: `-mb volume <0-100>`", mention_author=False)
                return
            level = int(argument)
            state["volume"] = level
            if player is not None and player.connected:
                await player.set_volume(level)
            await message.reply(f"Volume set to **{level}%**.", mention_author=False)
            return

        if command_name == "lyrics":
            if argument.lower() in {"on", "true", "yes"}:
                enabled = True
            elif argument.lower() in {"off", "false", "no"}:
                enabled = False
            elif not argument:
                enabled = not state.get("lyrics_enabled", False)
            else:
                await message.reply("Usage: `-mb lyrics [on|off]`", mention_author=False)
                return
            state["lyrics_enabled"] = enabled
            if enabled:
                await message.reply("Warning: This Feature Is In Beta, Don't Expect A Fully Working Version Soon", mention_author=False)
            else:
                await cancel_lyrics(guild_id)
                await message.reply("Lyrics disabled.", mention_author=False)
            current = state.get("current")
            player = state.get("player")
            if enabled and current is not None and current.get("lyrics") and player is not None:
                await cancel_lyrics(guild_id)
                state["lyrics_task"] = asyncio.create_task(
                    lyrics_loop(guild_id, player, player.channel, current)
                )
            return

        if command_name == "stop":
            state["queue"].clear()
            await cancel_lyrics(guild_id)
            if player is not None and player.playing:
                state["stopping"] = True
                try:
                    await player.stop()
                finally:
                    state["stopping"] = False
            state["current"] = None
            await message.reply("Stopped playback and cleared the queue.", mention_author=False)
            return

        await message.reply("Unknown command. Use `-mb help` to see available commands.", mention_author=False)
    except Exception as exc:
        logger.exception("Prefix command failed (%s): %s", command_name, exc)
        await message.reply("That command failed. Check the bot console for details.", mention_author=False)


@bot.tree.command(name="uptime", description="Show how long the bot has been online.")
async def bot_uptime(interaction: discord.Interaction) -> None:
    await send_interaction_response(
        interaction,
        f"I have been online for **{format_uptime()}**.",
        ephemeral=True,
    )


@bot.tree.command(name="update", description="Post the bot update log in this channel.")
async def bot_update(interaction: discord.Interaction) -> None:
    # Defer publicly so the README is posted in the channel, not as an ephemeral reply.
    await interaction.response.defer(thinking=True)
    try:
        count = await post_updates(interaction.channel)
    except Exception as exc:
        logger.warning("Could not post update log from slash command: %s", exc)
        await interaction.followup.send(
            "I couldn't fetch the update log. Please try again later.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"Posted the update log in {count} message(s).",
        ephemeral=True,
    )


@bot.tree.command(name="commands", description="List the bot's slash and -mb text commands.")
async def bot_commands(interaction: discord.Interaction) -> None:
    listing = (
        "**Music commands**\n"
        "`/play <song>` · `/pause` · `/resume` · `/skip` · `/queue`\n"
        "`/volume <0-100>` · `/lyrics [enabled]` · `/stop` · `/join` · `/leave`\n\n"
        "**Other slash commands**\n"
        "`/uptime` · `/update` · `/commands` · `/say <message>`\n"
        "`/channel link` · `/movie list` · `/movie search <title>` · `/movie play <movie>`\n"
        "`/sync` (administrator)\n"
        "Movie features are slash commands; they do not currently have `-mb` text equivalents.\n\n"
        "**Text command equivalents**\n"
        "`-mb play <song>` · `-mb pause` · `-mb resume` · `-mb skip` · `-mb queue`\n"
        "`-mb volume <0-100>` · `-mb lyrics [on|off]` · `-mb stop` · `-mb join` · `-mb leave`\n"
        "`-mb uptime` · `-mb update` · `-mb commands` · `-mb say <message>` · `-mb sync`\n"
        "The `say` commands require Manage Messages permission. The `sync` commands require Administrator permission."
    )
    await send_interaction_response(interaction, listing, ephemeral=True)


@bot.tree.command(name="say", description="Make the bot post a message in this channel.")
@app_commands.describe(message="The text the bot should post.")
@app_commands.checks.has_permissions(manage_messages=True)
async def bot_say(interaction: discord.Interaction, message: str) -> None:
    text_to_send = message.strip()
    if not text_to_send:
        await send_interaction_response(interaction, "Please provide a message to send.", ephemeral=True)
        return
    if len(text_to_send) > 2000:
        await send_interaction_response(interaction, "Messages must be 2,000 characters or fewer.", ephemeral=True)
        return
    await interaction.response.send_message(
        text_to_send,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.event
async def on_wavelink_node_ready(payload) -> None:
    global lavalink_ready
    lavalink_ready = True
    logger.info("Lavalink node ready: %s (resumed=%s)", payload.node.identifier, payload.resumed)


@bot.event
async def on_wavelink_node_disconnected(payload) -> None:
    global lavalink_ready
    lavalink_ready = False
    logger.warning("Lavalink node disconnected: %s", getattr(getattr(payload, "node", None), "identifier", "unknown"))

@bot.event
async def on_wavelink_node_closed(node, disconnected) -> None:
    global lavalink_ready
    lavalink_ready = False
    logger.warning(
        "Lavalink node closed: %s; disconnected players=%d",
        getattr(node, "identifier", "unknown"),
        len(disconnected or []),
    )


@bot.event
async def on_wavelink_track_start(payload) -> None:
    player = getattr(payload, "player", None)
    if player is None or player.guild is None:
        return
    state = music_states.get(player.guild.id)
    if state is None or state.get("current") is None:
        return
    await cancel_lyrics(player.guild.id)
    track = state["current"]
    if track.get("lyrics") and state.get("lyrics_enabled", False):
        state["lyrics_task"] = asyncio.create_task(
            lyrics_loop(player.guild.id, player, player.channel, track)
        )


@bot.event
async def on_wavelink_track_end(payload) -> None:
    player = getattr(payload, "player", None)
    if player is None or player.guild is None:
        return
    state = music_states.get(player.guild.id)
    if state is None:
        return
    await cancel_lyrics(player.guild.id)
    state["current"] = None
    if state.get("stopping"):
        return
    await play_next(player.guild.id)


# Playback-position diagnostics: detects stalls even when Lavalink does not
# emit a dedicated TrackStuck event.
_playback_position_watch: dict[int, dict] = {}


@bot.event
async def on_wavelink_player_update(payload) -> None:
    player = getattr(payload, "player", None)
    guild = getattr(player, "guild", None)
    if player is None or guild is None:
        return

    guild_id = guild.id
    state = music_states.get(guild_id)
    if not state or not state.get("current") or not getattr(player, "playing", False):
        _playback_position_watch.pop(guild_id, None)
        return

    position = getattr(payload, "position", None)
    if position is None:
        position = getattr(player, "position", None)
    if position is None:
        return

    now = time.monotonic()
    watch = _playback_position_watch.get(guild_id)
    if watch is None or position > watch["position"]:
        _playback_position_watch[guild_id] = {
            "position": position,
            "last_advance": now,
            "reported": False,
        }
        return

    # Player updates arrive periodically. Log only once per stall, and only
    # after 10 seconds without forward progress; do not restart or skip.
    stalled_for = now - watch["last_advance"]
    if stalled_for >= 10 and not watch["reported"]:
        current = state.get("current") or {}
        logger.warning(
            "Playback position stalled: guild=%s track=%s position_ms=%s stalled_for=%.1fs "
            "player_playing=%s connected=%s",
            guild_id,
            current.get("title", "unknown"),
            position,
            stalled_for,
            getattr(player, "playing", "unknown"),
            getattr(player, "connected", "unknown"),
        )
        watch["reported"] = True


@bot.event
async def on_wavelink_track_stuck(payload) -> None:
    """Log Lavalink playback stalls without restarting or skipping the track."""
    player = getattr(payload, "player", None)
    track = getattr(payload, "track", None)
    guild = getattr(player, "guild", None)
    threshold = getattr(payload, "threshold", None)
    if threshold is None:
        threshold = getattr(payload, "threshold_ms", None)
    position = getattr(player, "position", None)

    logger.warning(
        "Lavalink track stuck: guild=%s track=%s position_ms=%s threshold=%s",
        getattr(guild, "id", "unknown"),
        getattr(track, "title", "unknown"),
        position if position is not None else "unknown",
        threshold if threshold is not None else "unknown",
    )


@bot.event
async def on_wavelink_track_exception(payload) -> None:
    player = getattr(payload, "player", None)
    if player is None or player.guild is None:
        return
    guild_id = player.guild.id
    logger.warning(
        "Lavalink track exception in guild %s for %s: %s",
        guild_id,
        getattr(getattr(payload, "track", None), "title", "unknown"),
        getattr(getattr(payload, "exception", None), "message", "unknown error"),
    )

    # A failed track may not emit track_end. Clear it and continue the queue
    # so one unavailable stream does not stall all later tracks.
    state = music_states.get(guild_id)
    if state is None:
        return
    await cancel_lyrics(guild_id)
    async with state["advance_lock"]:
        state["current"] = None
    if not state.get("stopping"):
        await play_next(guild_id)


@bot.event
async def on_wavelink_websocket_closed(payload) -> None:
    logger.warning(
        "Discord voice websocket closed through Lavalink: guild=%s code=%s reason=%s remote=%s",
        getattr(getattr(payload, "player", None), "guild", None),
        getattr(payload, "code", "unknown"),
        getattr(payload, "reason", "unknown"),
        getattr(payload, "by_remote", "unknown"),
    )

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
        await send_interaction_response(
            interaction,
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    try:
        # Remove the guild-scoped copies first. This bot already syncs its
        # canonical command tree globally on startup, so copying the global
        # tree into the guild creates a second set of visible commands.
        bot.tree.clear_commands(guild=interaction.guild)
        await bot.tree.sync(guild=interaction.guild)

        # Keep the existing global sync mechanism as the single source of truth.
        synced = await bot.tree.sync()
        logger.info(
            "Cleared guild-specific commands and synced %d global slash command(s) for %s (%s).",
            len(synced),
            interaction.guild.name,
            interaction.guild.id,
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.warning("Could not reset/sync slash commands: %s", exc)
        await send_interaction_response(
            interaction,
            "I could not reset and sync the commands. Check the bot's application-command permissions and try again.",
            ephemeral=True,
        )
        return

    await send_interaction_response(
        interaction,
        f"Removed duplicate server-specific commands and synced {len(synced)} global slash command(s). They may take a little while to refresh.",
        ephemeral=True,
    )


bot.tree.add_command(channel_group)
bot.tree.add_command(movie_group)



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
