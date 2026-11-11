"""Where the plugin keeps things, and whether this computer has a screen: what both its MCP server and its page
(web.py) need, without the browser."""

import os
import sys
from pathlib import Path


def data() -> Path:
    """The plugin's data folder (BROWSER_DATA, lo9i's {data})."""
    return Path(os.environ.get("BROWSER_DATA") or Path.home() / ".lo9i" / "data" / "browser")


def live_socket(folder: Path) -> Path:
    """Where the browser is shown live while it shows on a computer without a screen (live.py, web.py)."""
    return folder / "live.sock"


def has_screen(platform: str = sys.platform) -> bool:
    """Whether a window can show on this computer: a Linux one needs a display (a server has none)."""
    return platform != "linux" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
