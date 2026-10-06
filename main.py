import asyncio
import json
import logging
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
MOVIES_DIR = BASE_DIR / "Movies"
CONFIG_FILE = BASE_DIR / "config.json"
SUPPORTED_EXTENSIONS = {".mp4", ".mp3"}
M3U8_SUFFIX = ".m3u8"
DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
HOST_EXPIRY_BUFFER_SECONDS = 30
WEB_HOST = os.getenv("MOVIE_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("MOVIE_PORT", "8080"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

active_host = None
host_lock = asyncio.Lock()

load_dotenv()

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
            await channel.send(embed=embed)
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


async def clear_hosted_movie(token: str) -> None:
    global active_host
    async with host_lock:
        if active_host is None or active_host["token"] != token:
            return
        task = active_host.get("task")
        active_host = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
    logger.info("Hosted movie expired.")


async def expire_hosted_movie(token: str, duration: float) -> None:
    await asyncio.sleep(duration + HOST_EXPIRY_BUFFER_SECONDS)
    await clear_hosted_movie(token)


async def host_movie(movie: Path, progress=None) -> tuple[bool, str]:
    global active_host

    if not PUBLIC_BASE_URL:
        return False, "Movie streaming is not configured. Set PUBLIC_BASE_URL to the public URL of this bot's web server."

    try:
        resolved = movie.resolve()
        if resolved.parent != MOVIES_DIR.resolve() or resolved.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return False, "That file is not a supported movie in the Movies folder."
        if not resolved.exists() or not resolved.is_file():
            return False, "That movie is no longer available."
    except OSError:
        return False, "I could not access that movie."

    if progress is not None:
        await progress("Preparing movie player...")

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
        active_host = {
            "token": token,
            "movie": resolved,
            "expires_at": asyncio.get_running_loop().time() + duration + HOST_EXPIRY_BUFFER_SECONDS,
            "task": None,
        }
        active_host["task"] = asyncio.create_task(expire_hosted_movie(token, duration))

    return True, f"{PUBLIC_BASE_URL}/movie/{token}"


async def hosted_movie_handler(request: web.Request) -> web.StreamResponse:
    async with host_lock:
        current = active_host
        token = request.match_info.get("token", "")
        if current is None or not secrets.compare_digest(current["token"], token):
            return web.Response(status=404, text="Movie is no longer being hosted.")
        movie = current["movie"]
        try:
            resolved = movie.resolve()
            if resolved.parent != MOVIES_DIR.resolve() or not resolved.is_file():
                return web.Response(status=404, text="Movie is no longer available.")
        except OSError:
            return web.Response(status=404, text="Movie is no longer available.")

    content_type = "video/mp4" if movie.suffix.lower() == ".mp4" else "audio/mpeg"
    return web.FileResponse(
        path=movie,
        headers={
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "Accept-Ranges": "bytes",
        },
    )


async def start_movie_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/movie/{token}", hosted_movie_handler)
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
        try:
            synced = await self.tree.sync()
            logger.info("Synced %d slash command(s).", len(synced))
        except discord.HTTPException as exc:
            logger.error("Failed to sync slash commands: %s", exc)

    async def close(self) -> None:
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


async def download_m3u8(url: str, progress=None) -> tuple[bool, str, Path | None]:
    ensure_movies_dir()

    if not is_m3u8_url(url):
        return False, "That is not a valid HTTP/HTTPS M3U8 URL.", None

    if progress is not None:
        await progress("Downloading movie...")

    filename = safe_download_name(url)
    output = MOVIES_DIR / filename
    temp_output = MOVIES_DIR / f".{filename}.part"

    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-protocol_whitelist",
            "http,https,tcp,tls,crypto",
            "-allowed_extensions",
            "ALL",
            "-extension_picky",
            "0",
            "-http_persistent",
            "1",
            "-i",
            url,
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(temp_output),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return (
            False,
            "FFmpeg is not installed or is not in PATH. Install FFmpeg and restart the bot.",
            None,
        )
    except OSError as exc:
        logger.warning("Could not start FFmpeg: %s", exc)
        return False, "I could not start the M3U8 download.", None

    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        temp_output.unlink(missing_ok=True)
        return False, "The M3U8 download timed out after 30 minutes.", None

    if process.returncode != 0 or not temp_output.exists():
        error_text = stderr.decode("utf-8", errors="replace").strip()
        logger.warning("FFmpeg M3U8 download failed: %s", error_text[-2000:])
        temp_output.unlink(missing_ok=True)
        return (
            False,
            "FFmpeg could not download that M3U8 stream. The URL may be expired, protected, or incompatible.",
            None,
        )

    try:
        temp_output.replace(output)
    except OSError as exc:
        logger.warning("Could not finalize downloaded movie: %s", exc)
        temp_output.unlink(missing_ok=True)
        return False, "The download completed, but I could not save the movie.", None

    return True, f"Downloaded {output.stem} into the Movies folder.", output


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

        success, message, downloaded = await download_m3u8(movie, progress)

        if not success or downloaded is None:
            await interaction.followup.send(message, ephemeral=True)
            return

        success, player_url = await host_movie(downloaded)
        if not success:
            await interaction.followup.send(f"{message} However, I could not host it: {player_url}", ephemeral=True)
            return

        channel = await get_movie_channel()
        if channel is None:
            await clear_hosted_movie(active_host["token"] if active_host else "")
            await interaction.followup.send("The movie was downloaded, but the configured movie channel is unavailable.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Now Playing: {downloaded.stem}",
            description=f"[▶ Watch Movie]({player_url})",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Format", value=downloaded.suffix.lower().lstrip(".").upper())
        embed.set_footer(text="This movie link expires automatically when the movie ends.")
        try:
            await channel.send(embed=embed)
            await interaction.followup.send(f"{message} Now hosting {downloaded.stem} in {channel.mention}.", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            await clear_hosted_movie(active_host["token"] if active_host else "")
            await interaction.followup.send("The movie was downloaded, but I could not post the movie player.", ephemeral=True)
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
        description=f"[▶ Watch Movie]({message})",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Format", value=selected.suffix.lower().lstrip(".").upper())
    embed.set_footer(text="This movie link expires automatically when the movie ends.")
    try:
        await channel.send(embed=embed)
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
