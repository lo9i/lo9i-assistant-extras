import base64
import hashlib
import io
import tarfile

import pytest

from copilot_tasks import cli


def _archive(content: bytes = b"#!/bin/sh\necho copilot\n") -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        info = tarfile.TarInfo(cli._MEMBER)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return data.getvalue()


def _integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def test_the_platform_package_follows_the_system_and_machine():
    assert cli.platform_name("darwin", "arm64") == "darwin-arm64"
    assert cli.platform_name("linux", "x86_64", musl=False) == "linux-x64"
    assert cli.platform_name("linux", "aarch64", musl=True) == "linuxmusl-arm64"
    with pytest.raises(cli.CliError, match="isn't published for win32"):
        cli.platform_name("win32", "x86_64")


def test_a_download_must_match_the_registrys_checksum():
    archive = _archive()
    cli.verify(archive, _integrity(archive))
    with pytest.raises(cli.CliError, match="doesn't match"):
        cli.verify(archive + b"x", _integrity(archive))
    with pytest.raises(cli.CliError, match="Unexpected integrity"):
        cli.verify(archive, "sha1-abc")


async def test_the_cli_is_downloaded_once_per_version(tmp_path, monkeypatch):
    downloads = []

    async def download(package, version):
        downloads.append((package, version))
        return _archive()

    monkeypatch.setattr(cli, "_download", download)
    steps = []
    binary = await cli.ensure(tmp_path, steps.append, version="9.9.9")
    assert binary == tmp_path / "9.9.9" / "copilot" and binary.stat().st_mode & 0o111
    assert await cli.ensure(tmp_path, steps.append, version="9.9.9") == binary
    assert len(downloads) == 1 and downloads[0][0].startswith("@github/copilot-")
    assert len(steps) == 1 and "Downloading the Copilot CLI 9.9.9" in steps[0].text
    assert [p.name for p in binary.parent.iterdir()] == ["copilot"]  # no partial file left
