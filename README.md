# Movie Bot

A Python Discord bot that lets members browse media files stored locally in the repository's Movies folder and send selected files to a configured Discord text channel.

The bot does not download movies from the internet. Only files manually placed in Movies are available.

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
- Graceful Discord upload and permission errors

## Requirements

- Python 3.10 or newer
- A Discord bot application/token
- The bot must be able to view and send messages in the configured movie channel
- The bot needs Send Messages and Attach Files permissions in that channel

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

The bot syncs its slash commands when it starts.

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

The bot does not download, execute, convert, or generate media files.

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
