import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

logger = logging.getLogger("movie-bot.lavalink")

LAVALINK_VERSION = "4.2.2"
YOUTUBE_PLUGIN_VERSION = "1.18.2"
JAVA_MAJOR = 21
LAVALINK_PORT = 2333
LAVALINK_HOST = "127.0.0.1"

RUNTIME_DIR = Path(__file__).resolve().parent / ".lavalink"
LAVALINK_JAR = RUNTIME_DIR / "Lavalink.jar"
PLUGINS_DIR = RUNTIME_DIR / "plugins"
CONFIG_FILE = RUNTIME_DIR / "application.yml"
JAVA_DIR = RUNTIME_DIR / "java"

LAVALINK_URL = (
    f"https://github.com/lavalink-devs/Lavalink/releases/download/"
    f"{LAVALINK_VERSION}/Lavalink.jar"
)
_process: asyncio.subprocess.Process | None = None
_log_task: asyncio.Task | None = None


def _java_executable() -> Path | None:
    """Find Java supplied by the hosting environment.

    Do not use .lavalink/java here. Bot-hosting.net has a small disk quota,
    and downloading/extracting a second JVM caused the previous deployment
    to consume excessive disk and memory.
    """
    exe = "java.exe" if os.name == "nt" else "java"
    candidates: list[Path] = []

    java_home = os.getenv("JAVA_HOME", "").strip()
    if java_home:
        candidates.append(Path(java_home) / "bin" / exe)

    existing = shutil.which(exe)
    if existing:
        candidates.append(Path(existing))

    seen: set[str] = set()
    for candidate in candidates:
        try:
            candidate = candidate.expanduser()
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)

        if key in seen or not candidate.is_file():
            continue
        seen.add(key)

        try:
            completed = subprocess.run(
                [str(candidate), "-version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            match = __import__("re").search(r'version "([0-9]+)', output)
            if match and int(match.group(1)) >= 17:
                return candidate
        except (OSError, subprocess.SubprocessError, ValueError):
            continue

    return None


def _cleanup_private_java() -> None:
    """Delete any old bundled JVM left by previous Movie-Bot releases."""
    if not JAVA_DIR.exists():
        return

    try:
        shutil.rmtree(JAVA_DIR)
        logger.info("Removed unused private Java runtime to recover disk space.")
    except OSError as exc:
        logger.warning("Could not remove old private Java runtime: %s", exc)


def _ensure_java() -> Path:
    # Always use the host-provided JVM. Never download another JVM into the
    # bot's storage volume.
    java = _java_executable()

    if java is None:
        raise RuntimeError(
            "Movie-Bot requires Java 17 or newer for Lavalink, but no host Java "
            "runtime was found. Install/provide Java 17+ in the hosting panel "
            "(JAVA_HOME or PATH) instead of downloading a private JVM."
        )

    # Clean up a JVM left behind by older versions after we have selected the
    # host JVM. This prevents the returned executable from ever pointing into
    # the directory we are deleting.
    _cleanup_private_java()

    # Verify the executable still exists after cleanup. This also protects
    # against unusual hosting setups where JAVA_HOME points at .lavalink/java.
    if not java.is_file():
        raise RuntimeError(
            f"The configured host Java executable disappeared during cleanup: {java}"
        )

    logger.info("Using host Java for Lavalink: %s", java)
    return java

def _cleanup_legacy_plugin_copy() -> None:
    """Remove the plugin copy created by older Movie-Bot builds."""
    if not PLUGINS_DIR.exists():
        return
    for plugin in PLUGINS_DIR.glob("youtube-plugin-*.jar"):
        try:
            plugin.unlink()
            logger.info("Removed legacy manually-downloaded YouTube Source plugin copy.")
        except OSError as exc:
            logger.warning("Could not remove legacy YouTube plugin copy: %s", exc)


def _cleanup_runtime_logs() -> None:
    """Prevent Lavalink's rolling logs from consuming the bot's disk quota."""
    logs_dir = RUNTIME_DIR / "logs"
    if not logs_dir.exists():
        return

    total = 0
    for item in logs_dir.glob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass

    # Logs are diagnostic only. Never allow an old Lavalink instance to
    # consume an entire hosting volume.
    if total > 10 * 1024 * 1024:
        logger.warning("Lavalink logs exceeded 10 MB; clearing old runtime logs.")
        try:
            shutil.rmtree(logs_dir)
        except OSError as exc:
            logger.warning("Could not clear Lavalink logs: %s", exc)


def _write_config(password: str) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

    config = f"""server:
  address: {LAVALINK_HOST}
  port: {LAVALINK_PORT}

lavalink:
  plugins:
    - dependency: "dev.lavalink.youtube:youtube-plugin:{YOUTUBE_PLUGIN_VERSION}"
      snapshot: false
  pluginsDir: "./plugins"
  server:
    password: "{password}"
    sources:
      youtube: false
      bandcamp: false
      soundcloud: true
      twitch: false
      vimeo: false
      nico: false
      http: false
      local: false
    filters:
      volume: true
      equalizer: false
      karaoke: false
      timescale: false
      tremolo: false
      vibrato: false
      distortion: false
      rotation: false
      channelMix: false
      lowPass: false
    bufferDurationMs: 100
    frameBufferDurationMs: 1000
    opusEncodingQuality: 5
    resamplingQuality: LOW
    trackStuckThresholdMs: 10000
    useSeekGhosting: true
    playerUpdateInterval: 5
    youtubePlaylistLoadLimit: 2
    youtubeSearchEnabled: true
    soundcloudSearchEnabled: false
    gc-warnings: false
    timeouts:
      connectTimeoutMs: 3000
      connectionRequestTimeoutMs: 3000
      socketTimeoutMs: 3000

plugins:
  youtube:
    enabled: true
    allowSearch: true
    allowDirectVideoIds: true
    allowDirectPlaylistIds: true
    clients:
      - MUSIC
      - WEB
      - WEBEMBEDDED

logging:
  file:
    path: ./logs/
  level:
    root: WARN
    lavalink: WARN
  logback:
    rollingpolicy:
      max-file-size: 2MB
      max-history: 1
"""
    CONFIG_FILE.write_text(config, encoding="utf-8")


async def _download_runtime_files() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

    if not LAVALINK_JAR.exists():
        logger.info("Downloading Lavalink %s...", LAVALINK_VERSION)
        await asyncio.to_thread(_download, LAVALINK_URL, LAVALINK_JAR)

    # Lavalink downloads the declared YouTube Source plugin into pluginsDir.
    # Do not download a second copy here; that would waste disk space.


async def start_lavalink(password: str) -> None:
    global _process, _log_task

    if _process is not None and _process.returncode is None:
        return

    java = await asyncio.to_thread(_ensure_java)
    await _download_runtime_files()
    await asyncio.to_thread(_cleanup_legacy_plugin_copy)
    await asyncio.to_thread(_cleanup_runtime_logs)
    await asyncio.to_thread(_write_config, password)

    logger.info(
        "Starting self-hosted Lavalink %s on %s:%s...",
        LAVALINK_VERSION,
        LAVALINK_HOST,
        LAVALINK_PORT,
    )

    # Bot-hosting services commonly enforce tight memory/CPU quotas. Keep
    # Lavalink deliberately small so the Discord bot and movie server retain
    # resources. The values can be overridden for larger hosts.
    xms = os.getenv("LAVALINK_XMS", "48m")
    xmx = os.getenv("LAVALINK_XMX", "192m")
    metaspace = os.getenv("LAVALINK_MAX_METASPACE", "64m")

    _process = await asyncio.create_subprocess_exec(
        str(java),
        f"-Xms{xms}",
        f"-Xmx{xmx}",
        f"-XX:MaxMetaspaceSize={metaspace}",
        "-XX:ActiveProcessorCount=1",
        "-XX:+UseSerialGC",
        "-jar",
        str(LAVALINK_JAR),
        cwd=str(RUNTIME_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    _log_task = asyncio.create_task(_forward_logs(_process))


async def _forward_logs(process: asyncio.subprocess.Process) -> None:
    if process.stdout is None:
        return
    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            logger.info("[Lavalink] %s", line.decode("utf-8", errors="replace").rstrip())
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Lavalink log reader stopped: %s", exc)


async def wait_until_ready(timeout: float = 90.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    url = f"http://{LAVALINK_HOST}:{LAVALINK_PORT}/version"

    while asyncio.get_running_loop().time() < deadline:
        if _process is not None and _process.returncode is not None:
            raise RuntimeError(f"Lavalink exited during startup with code {_process.returncode}.")

        try:
            request = Request(url, headers={"User-Agent": "Movie-Bot/1.0"})
            await asyncio.to_thread(_check_http, request)
            return
        except Exception:
            await asyncio.sleep(1)

    raise TimeoutError("Timed out waiting for the self-hosted Lavalink server.")


def _check_http(request: Request) -> None:
    with urlopen(request, timeout=3) as response:
        response.read(1)


async def stop_lavalink() -> None:
    global _process, _log_task

    process = _process
    _process = None

    if _log_task is not None:
        _log_task.cancel()
        try:
            await _log_task
        except asyncio.CancelledError:
            pass
        _log_task = None

    if process is None:
        return

    if process.returncode is not None:
        return

    logger.info("Stopping self-hosted Lavalink...")
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
