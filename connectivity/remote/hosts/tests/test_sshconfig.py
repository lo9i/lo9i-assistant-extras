import shutil
from pathlib import Path

import pytest

from hosts import sshconfig
from hosts.sshconfig import Address


def test_each_host_line_is_one_host_named_after_its_first_name(config):
    assert sshconfig.names() == ["pi", "nas"]


def test_patterns_and_option_like_names_are_not_hosts(config):
    config.write_text("Host *.example !bad -oProxyCommand=x\nHost ??\nHost=web web2\n  HostName web.local\n")
    assert sshconfig.names() == ["web"]


def test_included_files_add_their_hosts_in_place(config):
    folder = Path(config).parent / "conf.d"
    folder.mkdir()
    (folder / "work.conf").write_text('Host "work box" jump\n')
    config.write_text("Host first\nInclude conf.d/*.conf\nHost last # the last one\n")
    assert sshconfig.names() == ["first", "work box", "last"]


def test_includes_that_loop_stop(config):
    config.write_text(f"Host a\nInclude {config}\n")
    assert sshconfig.names() == ["a"]


def test_no_config_means_no_hosts(config):
    config.unlink()
    assert sshconfig.names() == []


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs ssh, which reads the config offline")
async def test_where_a_host_goes_comes_from_ssh_itself():
    assert await sshconfig.resolve("pi") == Address("lo9i-pi.local", "lo9i", 22)
    assert await sshconfig.resolve("nas") == Address("192.168.0.10", "groso", 2222)


def test_an_address_reads_like_ssh_takes_it():
    assert str(Address("lo9i-pi.local", "lo9i", 22)) == "lo9i@lo9i-pi.local"
    assert str(Address("10.0.0.2", "", 2222)) == "10.0.0.2:2222"
