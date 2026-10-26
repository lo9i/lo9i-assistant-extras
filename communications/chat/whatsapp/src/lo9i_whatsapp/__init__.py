"""The WhatsApp channel: the user's own WhatsApp account, linked like WhatsApp Web, that lo9i runs as a plugin
process (app.py).

neonize imports python-magic, which needs the libmagic library, which a Mac doesn't have unless Homebrew put it
there. The channel never asks it what a file is (it sends only text), so without libmagic a stand-in that says
every file is binary takes its place, and neonize imports."""

import sys
import types


def _stand_in_for_libmagic() -> None:
    try:
        import magic  # noqa: F401
    except ImportError:
        stand_in = types.ModuleType("magic")
        stand_in.from_buffer = lambda buffer, mime=False: "application/octet-stream" if mime else "data"  # type: ignore[attr-defined]
        stand_in.from_file = lambda filename, mime=False: "application/octet-stream" if mime else "data"  # type: ignore[attr-defined]
        sys.modules["magic"] = stand_in


_stand_in_for_libmagic()
