"""Which browser runs the profile: Google Chrome when it's installed, else the system's Chromium on Linux
(Debian's /usr/bin/chromium), else the Chromium Playwright brings, downloaded into the plugin's data folder
the first time. Google's sign-in refuses browsers it takes for automated ones more often than Chrome itself,
so Chrome is the first choice.

A profile stays with the browser it was made with (`engine.json` beside it): opening it with another one,
older than the profile, can lose what it kept. Only when Chrome is gone does it move to Chromium."""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

CHROME = "chrome"
CHROMIUM = "chromium"
# The system's own Chromium, from the distribution's packages.
SYSTEM = "system"
# Where Playwright looks for Google Chrome (its channel "chrome") on the systems lo9i runs on.
CHROME_PATHS = {
    "darwin": Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    "linux": Path("/opt/google/chrome/chrome"),
}
SYSTEM_CHROMIUM_PATHS = {"linux": Path("/usr/bin/chromium")}
_INSTALL_SECONDS = 1800


class InstallError(Exception):
    pass


def chrome_installed(platform: str = sys.platform) -> bool:
    path = CHROME_PATHS.get(platform)
    return path is not None and path.exists()


def system_chromium(platform: str | None = None) -> Path | None:
    """The system's Chromium, when it's installed."""
    path = SYSTEM_CHROMIUM_PATHS.get(platform or sys.platform)
    return path if path is not None and path.exists() else None


def chosen(data: Path, chrome: bool, system: bool = False) -> str:
    """The browser for the profile in `data`: the one it was made with while that's still here, else
    Chrome when `chrome` (it's installed), else the system's Chromium when `system`, else Playwright's.
    Remembered for the next time."""
    record = data / "engine.json"
    try:
        kept = json.loads(record.read_text()).get("engine")
    except (OSError, ValueError, AttributeError):
        kept = None
    if not isinstance(kept, str):
        kept = None
    here = {CHROME: chrome, SYSTEM: system, CHROMIUM: True}
    engine = kept if here.get(kept) else CHROME if chrome else SYSTEM if system else CHROMIUM
    if engine != kept:
        data.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"engine": engine}))
    return engine


def launching(engine: str) -> dict:
    """What Playwright launches it with: its channel, or the system's Chromium by its path."""
    path = system_chromium() if engine == SYSTEM else None
    return {"executable_path": str(path)} if path is not None else {"channel": engine}


def browsers_dir(data: Path) -> Path:
    """Where Playwright keeps the Chromium it downloads (PLAYWRIGHT_BROWSERS_PATH)."""
    return data / "browsers"


async def install_chromium(data: Path) -> None:
    """Downloads Playwright's Chromium (about 150 MB) unless it's there; quick when it is."""
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browsers_dir(data))}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        "--no-shell",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), _INSTALL_SECONDS)
    except TimeoutError:
        proc.kill()
        raise InstallError("Downloading Chromium took longer than 30 minutes.") from None
    if proc.returncode != 0:
        said = stderr.decode(errors="replace").strip()[-500:]
        log.error("Chromium's download failed: %s", said)
        raise InstallError(f"Couldn't download Chromium: {said}")
