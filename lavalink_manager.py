import asyncio
import json
import logging
import os
import platform
import shutil
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
YOUTUBE_PLUGIN_URL = (
    f"https://github.com/lavalink-devs/youtube-source/releases/download/"
    f"{YOUTUBE_PLUGIN_VERSION}/youtube-plugin-{YOUTUBE_PLUGIN_VERSION}.jar"
)

_process: asyncio.subprocess.Process | None = None
_log_task: asyncio.Task | None = None


def _java_executable() -> Path | None:
    exe = "java.exe" if os.name == "nt" else "java"
    existing = shutil.which(exe)
    if existing:
        return Path(existing)

    candidate = JAVA_DIR / "bin" / exe
    if candidate.exists():
        return candidate
    return None


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "Movie-Bot/1.0"})
    with urlopen(request, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _adoptium_asset_url() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        os_name = "windows"
    elif system == "darwin":
        os_name = "mac"
    else:
        os_name = "linux"

    if machine in {"aarch64", "arm64"}:
        architecture = "aarch64"
    elif machine in {"x86_64", "amd64", "x64"}:
        architecture = "x64"
    elif machine in {"x86", "i386", "i686"}:
        architecture = "x32"
    else:
        raise RuntimeError(f"Unsupported CPU architecture for automatic Java setup: {machine}")

    api_url = (
        f"https://api.adoptium.net/v3/assets/latest/{JAVA_MAJOR}/hotspot"
        f"?architecture={architecture}&image_type=jre&os={os_name}&vendor=eclipse"
    )
    request = Request(api_url, headers={"User-Agent": "Movie-Bot/1.0"})
    with urlopen(request, timeout=30) as response:
        assets = json.load(response)

    if not assets:
        raise RuntimeError(f"Adoptium did not provide a Java {JAVA_MAJOR} JRE for {os_name}/{architecture}.")

    return assets[0]["binary"]["package"]["link"]


def _extract_java(archive: Path) -> None:
    JAVA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=RUNTIME_DIR) as temp:
        temp_dir = Path(temp)
        if archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(temp_dir)
        else:
            with tarfile.open(archive, "r:*") as tf:
                tf.extractall(temp_dir)

        java_candidates = list(temp_dir.rglob("java.exe" if os.name == "nt" else "java"))
        if not java_candidates:
            raise RuntimeError("The downloaded Java archive did not contain a Java executable.")

        java_bin = java_candidates[0]
        source_root = java_bin.parent.parent
        if JAVA_DIR.exists():
            shutil.rmtree(JAVA_DIR)
        shutil.copytree(source_root, JAVA_DIR)


def _ensure_java() -> Path:
    existing = _java_executable()
    if existing:
        return existing

    logger.info("No Java runtime found. Downloading a private Java %s runtime...", JAVA_MAJOR)
    archive_url = _adoptium_asset_url()
    suffix = ".zip" if platform.system().lower() == "windows" else ".tar.gz"
    archive = RUNTIME_DIR / f"java-{JAVA_MAJOR}{suffix}"
    if not archive.exists():
        _download(archive_url, archive)

    _extract_java(archive)
    try:
        archive.unlink()
    except OSError:
        pass

    java = _java_executable()
    if java is None:
        raise RuntimeError("Automatic Java installation completed, but the Java executable could not be found.")
    return java


def _write_config(password: str) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

    config = f"""server:
  address: {LAVALINK_HOST}
  port: {LAVALINK_PORT}

lavalink:
  server:
    password: "{password}"
    sources:
      youtube: false
      bandcamp: true
      soundcloud: true
      twitch: true
      vimeo: true
      http: true
      local: false
    filters:
      volume: true
      equalizer: true
      karaoke: true
      timescale: true
      tremolo: true
      vibrato: true
      distortion: true
      rotation: true
      channelMix: true
      lowPass: true
    bufferDurationMs: 400
    frameBufferDurationMs: 5000
    opusEncodingQuality: 10
    resamplingQuality: LOW
    trackStuckThresholdMs: 10000
    useSeekGhosting: true
    playerUpdateInterval: 5
    youtubePlaylistLoadLimit: 6
    gc-warnings: true

plugins:
  youtube:
    enabled: true
    allowSearch: true
    allowDirectVideoIds: true
    allowDirectPlaylistIds: true
    clients:
      - MUSIC
      - ANDROID_VR
      - WEB
      - WEBEMBEDDED

logging:
  level:
    root: INFO
    lavalink: INFO
"""
    CONFIG_FILE.write_text(config, encoding="utf-8")


async def _download_runtime_files() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

    if not LAVALINK_JAR.exists():
        logger.info("Downloading Lavalink %s...", LAVALINK_VERSION)
        await asyncio.to_thread(_download, LAVALINK_URL, LAVALINK_JAR)

    plugin = PLUGINS_DIR / f"youtube-plugin-{YOUTUBE_PLUGIN_VERSION}.jar"
    if not plugin.exists():
        logger.info("Downloading YouTube Source plugin %s...", YOUTUBE_PLUGIN_VERSION)
        await asyncio.to_thread(_download, YOUTUBE_PLUGIN_URL, plugin)


async def start_lavalink(password: str) -> None:
    global _process, _log_task

    if _process is not None and _process.returncode is None:
        return

    java = await asyncio.to_thread(_ensure_java)
    await _download_runtime_files()
    await asyncio.to_thread(_write_config, password)

    logger.info(
        "Starting self-hosted Lavalink %s on %s:%s...",
        LAVALINK_VERSION,
        LAVALINK_HOST,
        LAVALINK_PORT,
    )

    _process = await asyncio.create_subprocess_exec(
        str(java),
        "-jar",
        str(LAVALINK_JAR),
        "--spring.config.additional-location=" + str(CONFIG_FILE),
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
