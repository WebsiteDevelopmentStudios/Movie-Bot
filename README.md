# Movie Bot

A Python Discord bot that lets members browse media files stored locally in the repository's Movies folder and send selected files to a configured Discord text channel.

The bot can send manually stored movies and can download a user-provided HTTP/HTTPS M3U8 stream into Movies. It does not search for or discover movies automatically.

## Features

- /channel link <channel> for administrators
- Persistent movie-channel configuration
- /movie list with a private ephemeral movie browser
- Pagination for large movie libraries
- Select This Movie confirmation button
- /movie play <movie>
- Case-insensitive movie-name matching
- Supports .mp4 and .mp3
- Automatically detects added and removed files without restarting
- Rejects filesystem paths and path traversal
- Expiring web video/audio player for large files
- Only one movie is hosted at a time; the player expires automatically when the movie ends
- Graceful Discord upload and permission errors
- Voice music playback with /play, /skip, /queue, /join, /leave, and /volume
- Spotify track-link resolution and song search through yt-dlp
- Synchronized lyrics lookup with one lyric line sent at a time in supported voice-channel chat

## Requirements

- Python 3.10 or newer
- A Discord bot application/token
- The bot must be able to view and send messages in the configured movie channel
- The bot needs Send Messages and Attach Files permissions in that channel
- FFmpeg installed and available in PATH for voice playback
- PyNaCl for Discord voice support
- yt-dlp for music source lookup and audio extraction

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

The bot uses Lavalink as its music transport. The Discord bot does not directly download or stream YouTube audio from the WispByte/Render process, which keeps the music transport separate from the movie system and avoids depending on public Piped instances.

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

Spotify does not provide Discord bots with unrestricted full-track audio. For a Spotify URL, the bot uses Spotify oEmbed only for title/artist metadata and then asks Lavalink to resolve a playable source separately. The bot never requests a user's Spotify password or stores Spotify credentials.

### Lavalink configuration

A Lavalink v4 server is required for music playback. It should be reachable by the WispByte bot over the network.

Set these environment variables:

    LAVALINK_HOST=your-lavalink-host.example.com
    LAVALINK_PORT=2333
    LAVALINK_PASSWORD=your_lavalink_password
    LAVALINK_SECURE=false

Set LAVALINK_SECURE=true when the Lavalink endpoint is exposed through HTTPS/TLS.

The Lavalink server must have a working audio source configured for the searches used by this bot. The bot deliberately does not implement a collection of public Piped-instance fallbacks or YouTube anti-bot workarounds. If the Lavalink node cannot resolve the requested source, /play reports that the music backend could not load it.

Lavalink and the bot may be hosted separately. This is recommended when the WispByte bot container has a shared outbound IP that is frequently challenged by a media provider.

The music queue is maintained independently for each Discord guild. Lavalink owns the actual audio transport while the bot owns queue state, commands, lyrics, and user-facing messages.

Synchronized lyrics are fetched from LRCLIB. The lyrics task reads Lavalink's actual player position instead of using a separate wall-clock timer. Lavalink reports the same position while playback is paused, so pausing the song also pauses lyric progression. Skipping or leaving immediately cancels the old lyric task.

Volume is changed through Lavalink's player volume control; the audio pipeline is not restarted.

The bot acknowledges /play before connecting to voice or performing network resolution, preventing long source lookups from causing Discord interaction timeout errors.

## Link the movie channel

An administrator runs:

    /channel link #movies

The channel ID is stored in config.json, so the setting survives restarts.

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

The bot does not search for media or automatically discover downloads. User-provided M3U8 URLs are downloaded on request. Movies are never executed.

## List movies

Use:

    /movie list

The result is private to the member who used the command. It shows the currently available files without their extensions.

Choose a movie from the dropdown and press Select This Movie to send it.

The Movies folder is scanned when the command is used, so newly added or removed files are detected without restarting the bot.

## Play a movie directly

Use the movie name without its extension:

    /movie play Avatar

For example, Avatar matches Movies/Avatar.mp4.

Movie names are matched case-insensitively. The requested movie must exist in Movies; the bot never searches the internet or downloads a missing movie.

## Large movie streaming

Large movies do not need to be uploaded to Discord. The bot can host one local movie at a time through its built-in HLS web player and posts an embed with a Watch Movie link.

The hosted player prepares the movie as HLS segments instead of serving one giant download. The browser requests the playlist and individual short segments as playback progresses, similar to the basic streaming model used by services such as YouTube. MP4 playback uses hls.js when the browser does not provide native HLS support.

The hosted link uses a random, unguessable token and expires automatically after the detected media duration plus a small safety buffer. When it expires, the movie is no longer available, the temporary HLS segments are deleted, and another movie can be hosted.

The bot automatically starts a Cloudflare Quick Tunnel for the built-in web server. You do not need to own a domain or add DNS records.

The tunnel creates a temporary HTTPS address such as:

    https://random-words.trycloudflare.com

That address is detected automatically and used for movie player links. The tunnel forwards to the local MOVIE_PORT (8080 by default).

Install Cloudflare's `cloudflared` command and make sure it is available in PATH before running the bot. You can override the executable name/path with:

    CLOUDFLARED_BIN=cloudflared

Quick Tunnel URLs are temporary and normally change when the bot is restarted. If cloudflared is unavailable, Discord commands still start, but web movie hosting will be unavailable until cloudflared is installed and the bot is restarted.

Only one movie is intentionally hosted at a time. If someone tries to start another movie while one is active, the bot tells them to wait until the current movie expires.

The HLS player requests individual media segments as needed rather than requiring the complete movie before playback can begin. The temporary HLS cache is kept outside the Movies folder and is removed when the hosted movie expires.

## Discord upload limits

Discord limits the size of files that bots can upload as attachments. The exact limit depends on the Discord account/server features available at the time of upload and can change.

If a file is too large for the current Discord attachment limit, the bot reports the failure instead of crashing.

## Project structure

    Movie-Bot/
    ├── Movies/
    │   └── .gitkeep
    ├── main.py
    ├── requirements.txt
    ├── .env.example
    ├── .gitignore
    ├── config.json
    └── README.md

config.json contains only the linked channel ID. The Discord token is never stored there.

## Security

- The token is read from .env.
- .env is ignored by Git.
- Movie commands cannot select arbitrary filesystem paths.
- Only files directly inside Movies with supported extensions are considered.
- Movie files are opened only for upload; they are never executed.
- External downloads are not used.
