"""Certificates and keys picked with Browse in the page. A browser sends a file's content, never where it is, so
the file is kept in the plugin's data folder (db.data_dir), only the user's, under a name made from its content, and
its path is saved as a typed one is. A file no environment uses any more is removed."""

import hashlib
import os
from pathlib import Path

from . import db
from .service import InvalidError

KINDS = {"certificate": ".crt", "key": ".key"}
# Far above a certificate or key ARCA works with; more is another kind of file.
MAX_BYTES = 64 * 1024


def folder() -> Path:
    return db.data_dir() / "certificates"


def keep(environment: str, kind: str, name: str, content: bytes) -> str:
    """The path of the file picked as `environment`'s `kind`, saved there; whether it's one is checked on saving."""
    if not content:
        raise InvalidError(f"{name} is empty")
    if len(content) > MAX_BYTES:
        raise InvalidError(f"{name} is too large for a {kind}")
    path = _made() / f"{environment}-{kind}-{hashlib.sha256(content).hexdigest()[:16]}{KINDS[kind]}"
    if not path.exists():
        _write(path, content)
    return str(path)


def forget_unused(settings: dict[str, str]) -> None:
    """Removes the kept files the settings don't point at: replaced, or picked for a save that failed."""
    if not folder().is_dir():
        return
    used = {v for k, v in settings.items() if k.startswith(("certificate_path.", "key_path."))}
    for path in folder().iterdir():
        if str(path) not in used:
            path.unlink(missing_ok=True)


def _made() -> Path:
    path = folder()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _write(path: Path, content: bytes) -> None:
    """Readable only by the user from the start: a private key is never there for others, even for a moment."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return  # the same content, kept by another save meanwhile
    with os.fdopen(fd, "wb") as f:
        f.write(content)
