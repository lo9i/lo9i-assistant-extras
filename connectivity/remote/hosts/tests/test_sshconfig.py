from pathlib import Path

from hosts import sshconfig
from hosts.details import Address, Details


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
    # Kept under a name ssh takes as one word.
    assert [name for name, _ in sshconfig.hosts()] == ["first", "work-box", "last"]


def test_includes_that_loop_stop(config):
    config.write_text(f"Host a\nInclude {config}\n")
    assert sshconfig.names() == ["a"]


def test_no_config_means_no_hosts(config):
    config.unlink()
    assert sshconfig.names() == [] and sshconfig.hosts() == []


def test_each_host_gets_what_the_config_gives_it_as_ssh_reads_it(config):
    config.write_text(
        "AddKeysToAgent yes\n"
        "Host pi lo9i-pi\n"
        "  HostName %h.local\n"
        "  User lo9i\n"
        '  IdentityFile "~/.ssh/my keys/pi"\n'
        "  IdentityFile ~/.ssh/other\n"
        "  LocalForward 8080 localhost:80\n"
        "Host nas\n"
        "  HostName=192.168.0.10\n"
        "  Port 2222\n"
        "Match host pi\n"
        "  User matched\n"
        "Host * !nas\n"
        "  User everyone\n"
        "  ServerAliveInterval 30\n"
        "  LocalForward 9090 localhost:90\n"
    )
    found = dict(sshconfig.hosts())
    assert found["pi"] == Details(
        "pi.local",
        "lo9i",  # The first value wins: `Host *` comes after.
        22,
        "~/.ssh/my keys/pi",
        [
            ("AddKeysToAgent", "yes"),
            ("IdentityFile", "~/.ssh/other"),
            ("LocalForward", "8080 localhost:80"),
            ("ServerAliveInterval", "30"),
            ("LocalForward", "9090 localhost:90"),  # Adds to the first one, as ssh does.
        ],
    )
    # `Host * !nas` isn't nas's; the Match entry is no one's.
    assert found["nas"] == Details("192.168.0.10", "", 2222, "", [("AddKeysToAgent", "yes")])


def test_an_address_reads_like_ssh_takes_it():
    assert str(Address("lo9i-pi.local", "lo9i", 22)) == "lo9i@lo9i-pi.local"
    assert str(Address("10.0.0.2", "", 2222)) == "10.0.0.2:2222"


def test_importing_never_writes_to_the_config(config):
    before = (config.read_text(), config.stat().st_mtime_ns)
    sshconfig.hosts()
    assert (config.read_text(), config.stat().st_mtime_ns) == before
