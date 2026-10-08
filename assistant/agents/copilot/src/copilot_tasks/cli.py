"""The Copilot CLI: a single binary per platform, published on npm as `@github/copilot-<platform>`. It's
downloaded from the npm registry the first time a task needs it, checked against the registry's sha512
integrity, and kept in the plugin's data folder, so Node isn't needed. A plugin update that raises
VERSION downloads the new one."""

import asyncio
import base64
import hashlib
import io
import os
import platform
import sys
import tarfile
import uuid
from pathlib import Path

import httpx

from copilot_tasks.task_folder import Progress, note

VERSION = "1.0.91"
_REGISTRY = "https://registry.npmjs.org"
_MEMBER = "package/copilot"
_SYSTEMS = {"darwin": "darwin", "linux": "linux"}
_MACHINES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}


class CliError(Exception):
    pass


async def ensure(root: Path, progress: Progress, version: str = VERSION) -> Path:
    """The binary for this computer, downloaded first if it isn't here yet."""
    binary = root / version / "copilot"
    if binary.is_file():
        return binary
    package = f"@github/copilot-{platform_name()}"
    progress(note(f"Downloading the Copilot CLI {version} ({package}), once"))
    archive = await _download(package, version)
    await asyncio.to_thread(_install, archive, binary)
    return binary


def platform_name(system: str = sys.platform, machine: str = platform.machine(), musl: bool | None = None) -> str:
    """The npm package's platform: darwin-arm64, linux-x64, linuxmusl-arm64 (Alpine)…"""
    if system not in _SYSTEMS or machine.lower() not in _MACHINES:
        raise CliError(f"The Copilot CLI isn't published for {system} on {machine}.")
    name = _SYSTEMS[system]
    if name == "linux" and (_is_musl() if musl is None else musl):
        name = "linuxmusl"
    return f"{name}-{_MACHINES[machine.lower()]}"


def verify(data: bytes, integrity: str) -> None:
    """`integrity` is npm's "sha512-<base64 digest>"."""
    algorithm, _, expected = integrity.partition("-")
    if algorithm != "sha512":
        raise CliError(f"Unexpected integrity {integrity!r} from the npm registry.")
    if base64.b64encode(hashlib.sha512(data).digest()).decode() != expected:
        raise CliError("The downloaded Copilot CLI doesn't match the npm registry's checksum.")


async def _download(package: str, version: str) -> bytes:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=300), follow_redirects=True) as http:
        meta = await http.get(f"{_REGISTRY}/{package.replace('/', '%2f')}/{version}")
        meta.raise_for_status()
        dist = meta.json()["dist"]
        tarball = await http.get(dist["tarball"])
        tarball.raise_for_status()
    verify(tarball.content, dist["integrity"])
    return tarball.content


def _install(archive: bytes, binary: Path) -> None:
    """Written under a name of its own and renamed, so two tasks downloading at once don't clash."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.extractfile(_MEMBER)
        if member is None:
            raise CliError(f"The Copilot CLI package has no {_MEMBER}.")
        content = member.read()
    binary.parent.mkdir(parents=True, exist_ok=True)
    partial = binary.with_name(f"copilot.{uuid.uuid4().hex}.partial")
    partial.write_bytes(content)
    partial.chmod(0o755)
    os.replace(partial, binary)


def _is_musl() -> bool:
    """glibc names its version; musl (Alpine) doesn't."""
    try:
        return not os.confstr("CS_GNU_LIBC_VERSION")
    except (ValueError, OSError):
        return True
