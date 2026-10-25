"""ssh's askpass helper (connect.py): ssh runs it with what it asks as its argument, and reads the answer from its
output. A password prompt gets the host's password, which the plugin's process gives once over the socket in
HOSTS_ASKPASS_SOCKET. Anything else (a key's passphrase, whether to trust a new host key) is asked in the
terminal ssh runs in, when it has one; without one, it's refused (exit 1), so ssh stops instead of waiting.
"""

import os
import socket
import sys
import termios

# Where the plugin's process listens to give the password (connect.py). This module needs nothing else of the
# plugin's, so ssh waits as little as possible for it.
ASKPASS_SOCKET = "HOSTS_ASKPASS_SOCKET"
_TIMEOUT_SECONDS = 10


def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    answer = _password() if "password" in prompt.lower() else None
    if answer is None:
        answer = _from_terminal(prompt)
    if answer is None:
        sys.exit(1)
    sys.stdout.write(answer + "\n")


def _password() -> str | None:
    path = os.environ.get(ASKPASS_SOCKET, "")
    if not path:
        return None
    data = b""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_TIMEOUT_SECONDS)
            sock.connect(path)
            while chunk := sock.recv(4096):
                data += chunk
    except OSError:
        return None
    return data.decode(errors="replace").removesuffix("\n") if data else None


def _from_terminal(prompt: str) -> str | None:
    """The answer typed in ssh's terminal, without echoing it but for a yes/no question; None without one."""
    try:
        tty = open("/dev/tty", "r+")  # noqa: SIM115 - closed below, after the terminal's settings are back.
    except OSError:
        return None
    with tty:
        tty.write(prompt)
        tty.flush()
        if "yes/no" in prompt:
            return tty.readline().rstrip("\n")
        fd = tty.fileno()
        old = termios.tcgetattr(fd)
        quiet = [*old[:3], old[3] & ~termios.ECHO, *old[4:]]
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, quiet)
            answer = tty.readline().rstrip("\n")
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
            tty.write("\n")
        return answer
