"""A host's details: where it is (address, user, port), the path of its key, its other ssh options and its
password. The plugin keeps them encrypted in its database (db.py, vault.py), and writes them as an ssh config
for each connection (connect.py). A key is only ever its file's path: what's in the file is never read or kept.
"""

import json
import re
from dataclasses import asdict, dataclass, field

# A host's name: what ssh is given and the config's Host line say. No spaces, patterns or `@` (`a@b` is a user
# and a host to ssh), and never a leading `-`, which ssh would read as an option.
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")
# An address or a user: one word, nothing a config line or ssh's command line reads otherwise.
_WORD = re.compile(r"""^[^\s"'#\\-][^\s"'#\\]*$""")
_KEYWORD = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
# A keyword and its values, after a space or an "=".
_LINE = re.compile(r"([A-Za-z]\w*)(?:\s*=\s*|\s+)(.*)")
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
# The fields' own keywords, and the ones that would read hosts from elsewhere than the plugin's database.
_FIELDS = {"hostname": "Address", "user": "User", "port": "Port"}
_REFUSED = ("host", "match", "include")


@dataclass(frozen=True)
class Address:
    hostname: str
    user: str
    port: int

    def __str__(self) -> str:
        port = "" if self.port == 22 else f":{self.port}"
        return f"{self.user}@{self.hostname}{port}" if self.user else f"{self.hostname}{port}"


@dataclass(frozen=True)
class Details:
    hostname: str
    user: str = ""
    port: int = 22
    identity_file: str = ""
    # ssh config lines, (keyword, values as written), in their order.
    options: list[tuple[str, str]] = field(default_factory=list)
    password: str = ""

    @property
    def address(self) -> Address:
        return Address(self.hostname, self.user, self.port)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "Details":
        data = json.loads(text)
        return cls(**{**data, "options": [(k, v) for k, v in data.get("options", [])]})


class InvalidHostError(Exception):
    """What's wrong with a host's fields, by field."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def valid_name(name: str) -> bool:
    return bool(_NAME.match(name))


def parse(
    name: str, hostname: str, user: str = "", port: str = "", identity_file: str = "", options: str = "",
    password: str = "",
) -> tuple[str, Details]:  # fmt: skip
    """A host from what the user typed: its name and details. Raises InvalidHostError saying what to change."""
    errors: dict[str, str] = {}
    name, hostname, user, identity_file = name.strip(), hostname.strip(), user.strip(), identity_file.strip()
    if not name:
        errors["name"] = "Give it a name."
    elif not valid_name(name):
        errors["name"] = "Up to 63 letters, digits, . _ and -, starting with a letter or digit."
    if not hostname:
        errors["hostname"] = "Its address: a name like pi.local, or an IP address."
    elif not _WORD.match(hostname):
        errors["hostname"] = "One word: a name like pi.local, or an IP address."
    if user and not _WORD.match(user):
        errors["user"] = "One word, like pi or root."
    number = _port(port, errors)
    if identity_file.startswith("-----") or _CONTROL.search(identity_file) or '"' in identity_file:
        errors["identity_file"] = "The key file's path, like ~/.ssh/id_ed25519. Never the key itself."
    found = _options(options, errors)
    if "\n" in password or "\r" in password:
        errors["password"] = "A password is one line."
    if errors:
        raise InvalidHostError(errors)
    return name, Details(hostname, user, number, identity_file, found, password)


def _port(text: str, errors: dict[str, str]) -> int:
    text = text.strip()
    if not text:
        return 22
    try:
        port = int(text)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        errors["port"] = "A port from 1 to 65535; 22 if you leave it empty."
    return port


def _options(text: str, errors: dict[str, str]) -> list[tuple[str, str]]:
    """The other options, one `Keyword value` a line, as in an ssh config; blank lines and comments left out."""
    found: list[tuple[str, str]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        matched = _LINE.fullmatch(line)
        if matched is None or not _KEYWORD.match(matched.group(1)):
            errors["options"] = f"Line {number}: an option and its value, like ServerAliveInterval 30."
            break
        keyword, value = matched.group(1), matched.group(2).strip()
        problem = option_problem(keyword, value)
        if problem:
            errors["options"] = f"Line {number}: {problem}"
            break
        found.append((keyword, value))
    return found


def option_problem(keyword: str, value: str) -> str:
    """Why an option can't be kept, or "" when it can."""
    lowered = keyword.lower()
    if lowered in _FIELDS:
        return f"{keyword} has its own field ({_FIELDS[lowered]})."
    if lowered in _REFUSED:
        return f"{keyword} can't be used here: each host has its options here, and nothing is read from elsewhere."
    if _CONTROL.search(value) or not value:
        return f"{keyword} needs a value on its line."
    return ""


def options_text(options: list[tuple[str, str]]) -> str:
    """The options as the form shows them, one a line."""
    return "\n".join(f"{keyword} {value}" for keyword, value in options)


def config_block(name: str, details: Details) -> str:
    """The host as an ssh config's Host entry. Its password isn't in it: ssh gets that from connect.py's helper."""
    lines = [f"Host {name}", f"  HostName {details.hostname}"]
    if details.user:
        lines.append(f"  User {details.user}")
    lines.append(f"  Port {details.port}")
    if details.identity_file:
        lines.append(f"  IdentityFile {_quoted(details.identity_file)}")
    lines += [f"  {keyword} {value}" for keyword, value in details.options]
    return "\n".join(lines) + "\n"


def _quoted(value: str) -> str:
    return f'"{value}"' if any(c.isspace() for c in value) else value
