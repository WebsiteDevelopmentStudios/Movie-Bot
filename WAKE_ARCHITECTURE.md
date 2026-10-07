# Movie-Bot wake-on-command architecture

Movie-Bot is being split into an always-online Discord edge endpoint and a
sleeping Python backend.

## Flow

Discord -> Cloudflare Worker -> Render -> existing discord.py bot

The Cloudflare Worker verifies Discord's Ed25519 signature and immediately
acknowledges the interaction. It then forwards the original interaction to
Render.

Render's public HTTP request wakes a sleeping free web service. The service
runs the existing Movie-Bot code and dispatches the forwarded interaction into
the existing discord.py command tree.

The existing movie web server remains on an internal port. The Render bridge
proxies its movie, media, HLS, and parts URLs so movie links remain public
without requiring a local PC or Cloudflare Tunnel.

## Secrets

Cloudflare Worker:

- DISCORD_PUBLIC_KEY
- BOT_INTERACTION_URL
- WAKE_SECRET

Render:

- DISCORD_TOKEN
- WAKE_SECRET

WAKE_SECRET must be identical on both sides.

## Important

The Worker is the permanent lightweight endpoint. The Python process is the
sleeping compute service.

The first command after Render sleeps can take longer because the Python
service must cold-start.

Music processing remains temporary: yt-dlp audio is cached only while needed
by the existing bot and can be removed by its existing cleanup behavior.
