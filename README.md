# Movie Bot

A Python Discord bot with music commands and link-only movie sharing. Movie search uses TMDB to find titles and provides a link to the provider page. The bot does not scrape movie websites, extract media URLs, or download movie streams.

## Features

- /channel link <channel> for administrators
- Persistent movie-channel configuration
- /movie list with a private ephemeral movie browser
- /movie search <title> with a private TMDB movie search and selection menu
- Pagination for large movie libraries
- Select This Movie confirmation button
- /movie play <URL> to share a movie page link without downloading media
- Search results use TMDB movie IDs to open the matching VidNest page at https://vidnest.fun/movie/{movie_id}
- Movie page links are shared as links only; the bot does not extract or download the video
- Graceful Discord upload and permission errors
- Voice music playback with /play, /skip, /queue, /join, /leave, and /volume
- Spotify track-link metadata resolution followed by Lavalink source search (YouTube/SoundCloud), with the existing yt-dlp direct-audio fallback retained
- Synchronized lyrics lookup with one lyric line sent at a time in supported voice-channel chat

## Requirements

- Python 3.10 or newer
- A Discord bot application/token
- The bot must be able to view and send messages in the configured movie channel
- The bot needs Send Messages permission in that channel
- PyNaCl for Discord voice support

## Installation

Clone the repository and enter it:

    git clone https://github.com/WebsiteDevelopmentStudios/Movie-Bot.git
    cd Movie-Bot

Install dependencies:

    pip install -r requirements.txt


## Configure the bot token

Create a file named .env in the project root:

    DISCORD_TOKEN=your_bot_token_here

Never commit .env or your bot token to GitHub.

## Invite the bot

1. Open the Discord Developer Portal.
2. Create or select your bot application.
3. Add a bot user and copy its token into .env.
4. Open OAuth2 URL Generator.
5. Select the bot and applications.commands scopes.
6. Give the bot only the permissions it needs. It needs to view/send messages and attach files in the movie channel.
7. Invite it to your server.

## Run

From the repository directory:

    python main.py

## Voice music

The bot uses Lavalink as its music transport. The Discord bot does not directly download or stream## Voice music

The bot now runs its own Lavalink music backend. You do **not** need to create a second Lavalink server, expose port 2333, or configure any `LAVALINK_*` environment variables.

On startup, the bot automatically:

1. Checks for an existing Java 17+ runtime.
2. If Java is unavailable, downloads a private Java 21 JRE into `.lavalink/java/`.
3. Downloads Lavalink 4.2.2 if it is not already cached.
4. Downloads the YouTube Source 1.18.2 plugin if it is not already cached.
5. Generates a random local Lavalink password.
6. Starts Lavalink on `127.0.0.1:2333`.
7. Waits for Lavalink to become ready.
8. Connects Wavelink to the local server.

The generated password is only used between the bot and its local Lavalink process. It is not stored in Git, and the Lavalink port is bound to localhost.

The runtime files are stored in `.lavalink/` and are ignored by Git. On persistent hosts, they are reused after the first successful download. On ephemeral hosts, they are downloaded again automatically when the service starts.

Commands:

    /join
    /play <song>
    /queue
    /skip
    /pause
    /resume
    /volume <level>
    /lyrics
    /leave

Run /join while you are in a voice channel, or use /play while you are already in one. /play accepts normal song searches and Spotify track URLs.

Spotify does not provide Discord bots with unrestricted full-track audio, and Spotify OAuth does not grant a bot a playable audio stream. For a Spotify URL, the bot uses Spotify oEmbed only for title/artist metadata, then asks Lavalink to search supported music sources for a matching playable track. If those searches fail, the existing yt-dlp direct-audio fallback is attempted where the Lavalink host supports HTTP audio URLs. The bot never requests a user's Spotify password or stores Spotify credentials.

### Self-hosted Lavalink

Movie-Bot currently bundles its Lavalink bootstrap configuration around:

- Lavalink 4.2.2
- YouTube Source 1.18.2
- Java 21 when an existing Java 17+ installation is not available
- A conservative Lavalink profile capped at 256 MB heap, one active JVM processor, and small rolling logs for low-resource hosting

The YouTube Source plugin is configured with YouTube search enabled and the built-in Lavalink YouTube source disabled, as required by the plugin.

If YouTube rejects a particular request, the bot reports that the music source could not be loaded. The self-hosted Lavalink architecture removes the need for public Piped-instance fallbacks, but it cannot guarantee that an external media provider will accept every automated request.

The music queue is maintained independently for each Discord guild. Lavalink owns the actual audio transport while the bot owns queue state, commands, lyrics, and user-facing messages.

Synchronized lyrics are fetched from LRCLIB. The lyrics task reads Lavalink's actual player position instead of using a separate wall-clock timer. Lavalink reports the same position while playback is paused, so pausing the song also pauses lyric progression. Skipping or leaving immediately cancels the old lyric task.

Volume is changed through Lavalink's player volume control; the audio pipeline is not restarted.

The bot acknowledges /play before connecting to voice or performing network resolution, preventing long source lookups from causing Discord interaction timeout errors.

son, so the setting survives restarts.

If the configured channel is deleted or cannot be fetched, an administrator must link another channel.

## Add movies

Put media files directly inside:

    Movies/

Supported formats:

- .mp4
- .mp3

Example:

    Movies/
    ├── Avatar.mp4
    ├── Minecraft Movie.mp4
    └── Music.mp3

Do not put movie files in subfolders. Do not commit copyrighted or otherwise unauthorized media to a public repository.

The bot does not scrape movie pages, resolve hidden stream URLs, or download movie media. `/movie play` only posts a user-provided HTTP/HTTPS link to the configured movie channel.

## List movies

Use:

    /movie list

The result is private to the member who used the command. It shows the currently available files without their extensions.

Choose a movie from the dropdown and press Select This Movie to send it.

The Movies folder is scanned when the command is used, so newly added or removed files are detected without restarting the bot.

## Share a movie link

Use `/movie search <title>` to find a title. Select a result to get a provider-page link. To share another movie page, use `/movie play <URL>` with its HTTP/HTTPS page URL. This command posts the URL to the configured movie channel; it does not scrape the page, extract a stream, or download any media.

## Discord upload limits

Discord limits the size of files that bots can upload as attachments. The exact limit depends on the Discord account/server features available at the time of upload and can change.

If a file is too large for the current Discord attachment limit, the bot reports the failure instead of crashing.

## Project structure

    Movie-Bot/
    ├── Movies/
    │   └── .gitkeep
    ├── main.py
    ├── lavalink_manager.py
    ├── requirements.txt
    ├── .env.example
    ├── .gitignore
    ├── config.json
    └── README.md

The `.lavalink/` directory is created automatically at runtime and is intentionally not committed to Git.

config.json contains only the linked channel ID. The Discord token is never stored there.

## Security

- The token is read from .env.
- .env is ignored by Git.
- Movie commands cannot select arbitrary filesystem paths.
- Only files directly inside Movies with supported extensions are considered.
- Movie links are shared as provided; the bot does not scrape provider pages or download movie streams.

## Bot utility commands

The bot also provides these utility commands:

- `/uptime` shows how long the current bot process has been running.
- `/update` fetches `updates.txt` and posts the bot's update log in the channel, split into Discord-safe messages.
- `/commands` lists the main slash commands and their `-mb` text-command equivalents.
- `/say <message>` posts a message as the bot. It requires the `Manage Messages` permission and disables automatic mention notifications.

The music utility commands also work with the `-mb` prefix, for example `-mb uptime`, `-mb update`, `-mb commands`, and `-mb say Hello`. The `say` text command also requires `Manage Messages`.

For prefix commands, enable **Message Content Intent** in the Discord Developer Portal under **Bot → Privileged Gateway Intents**.
