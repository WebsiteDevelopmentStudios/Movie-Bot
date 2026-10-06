import json
import logging
import os
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
MOVIES_DIR = BASE_DIR / "Movies"
CONFIG_FILE = BASE_DIR / "config.json"
SUPPORTED_EXTENSIONS = {".mp4", ".mp3"}

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
        success, message = await send_movie(self.movie, interaction.user)

        if success:
            button.disabled = True
            await interaction.edit_original_response(content=message, view=self)
        else:
            await interaction.edit_original_response(content=message, view=self)


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

        channel = self.channel_select.values[0]
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
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        try:
            synced = await self.tree.sync()
            logger.info("Synced %d slash command(s).", len(synced))
        except discord.HTTPException as exc:
            logger.error("Failed to sync slash commands: %s", exc)

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


@movie_group.command(name="play", description="Send an available movie to the movie channel.")
@app_commands.describe(movie="Movie name, without the .mp4 or .mp3 extension.")
async def movie_play(interaction: discord.Interaction, movie: str) -> None:
    if not get_movie_files():
        await interaction.response.send_message(
            "No movies are currently available.",
            ephemeral=True,
        )
        return

    selected = find_movie(movie)
    if selected is None:
        await interaction.response.send_message(
            "That movie is not available. Use /movie list to see the available movies.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    success, message = await send_movie(selected, interaction.user)
    await interaction.followup.send(message, ephemeral=True)


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
