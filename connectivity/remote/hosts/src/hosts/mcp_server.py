"""MCP server over stdio: the user's hosts (the plugin's own list, service.py), and their Herdr session
(herdr.py), the same terminal the user sees on the Hosts page, and the SSH tunnels the user set up there, which
that page runs (page.py). lo9i starts it (see server.yaml) with HOSTS_DB, HOSTS_WEB_SOCKET and the plugin's key
(vault.py) set:

    HOSTS_DB=/path/hosts.db HOSTS_WEB_SOCKET=/path/page.sock LO9I_DATA_KEY=… hosts-mcp
"""

import asyncio
from functools import cache
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import db, page, service
from .errors import ConnectError, LockedError
from .herdr import Herdr, HerdrError, Probe
from .page import PageError
from .service import Host, UnknownHostError
from .ssh import OpenSsh

INSTRUCTIONS = """\
The user's Linux hosts, as they keep them on the Hosts page of lo9i's web app, and a terminal on each
that keeps running when the connection drops (a Herdr session named lo9i). Your commands run in a tab
of their own there, labeled assistant, which the user sees beside theirs on the Hosts page.

Start with list_hosts. run_command types a command in your tab and returns its output and exit code;
read_screen shows what your tab shows, or the pane the user has in front. Commands that change a host
(installing or removing software, editing files, restarting services, rebooting, deleting anything) need
the user's OK first: say what you'll run and why, and run it only after they agree. Read-only commands
(status, logs, disk space) need no OK.

The user's SSH tunnels (ports forwarded through a host by this Mac) are on the same page:
list_tunnels says which there are and how each is doing; start_tunnel and stop_tunnel switch one on
or off. Only the user adds or changes them, on the Hosts page.
"""

mcp = MCPServer("hosts", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True)
# What a command may get at most to end before run_command gives back what it has so far.
_MAX_WAIT_SECONDS = 600


@cache
def _herdr() -> Herdr:
    return Herdr(OpenSsh())


# What keeps a host from being reached, said to the assistant as the tool's error.
_PROBLEMS = (UnknownHostError, LockedError, ConnectError)


async def _shown(name: str) -> Host:
    try:
        return await service.host(name, shown_only=True)
    except _PROBLEMS as e:
        raise ToolError(str(e)) from None


@mcp.tool(annotations=READ_ONLY)
async def list_hosts(
    check: Annotated[bool, Field(description="Also connect to each one to see if it answers and runs Herdr")] = True,
) -> list[dict[str, Any]]:
    """The hosts the user shows on their Hosts page: name (what the other tools take), address
    (user@hostname:port) and when the user last opened its terminal. With check, also whether it
    answers now, has Herdr, and runs lo9i's Herdr session."""
    try:
        hosts = await service.shown_hosts()
        probes: list[Probe | None] = [None] * len(hosts)
        if check:
            probes = list(await asyncio.gather(*(_herdr().probe(h.name) for h in hosts)))
    except _PROBLEMS as e:
        raise ToolError(str(e)) from None
    return [_host(h, p) for h, p in zip(hosts, probes, strict=True)]


@mcp.tool()
async def run_command(
    host: Annotated[str, Field(description="The host's name from list_hosts")],
    command: Annotated[str, Field(description="A shell command line (bash or sh)")],
    timeout_seconds: Annotated[int, Field(ge=1, le=_MAX_WAIT_SECONDS, description="How long to wait for it")] = 60,
) -> dict[str, Any]:
    """Runs a command in the assistant's own tab of the host's terminal (the Herdr session the user sees, in a
    tab labeled assistant, with a POSIX sh), by typing it there, and returns its output and exit code. A
    command that changes the host (installs, removes, edits files, restarts or stops services, reboots,
    deletes) needs the user's OK first: ask before calling this, unless they asked for exactly this command.
    Not for programs that wait for input (editors, pagers, password prompts): one still running after the
    timeout comes back with exit_code null and the screen so far; read_screen later shows how it went."""
    await _shown(host)
    try:
        run = await _herdr().run(host, command, timeout_seconds)
    except (HerdrError, *_PROBLEMS) as e:
        raise ToolError(str(e)) from None
    return {"output": run.output, "exit_code": run.exit_code, "still_running": run.exit_code is None} | (
        {"note": "The start of the output scrolled out of the terminal before it was read."} if run.truncated else {}
    )


@mcp.tool(annotations=READ_ONLY)
async def read_screen(
    host: Annotated[str, Field(description="The host's name from list_hosts")],
    lines: Annotated[
        int, Field(ge=0, le=2000, description="Rows of scrollback to read; 0 for the screen as it is now")
    ] = 0,
    user_pane: Annotated[
        bool, Field(description="Read the pane the user has in front instead of the assistant's tab")
    ] = False,
) -> str:
    """What the assistant's tab of the host's terminal (the Herdr session the user sees) shows now, as text, or
    with user_pane the pane the user has in front."""
    await _shown(host)
    try:
        return await _herdr().screen(host, lines, user_pane)
    except (HerdrError, *_PROBLEMS) as e:
        raise ToolError(str(e)) from None


def _host(h: Host, probe: Probe | None) -> dict[str, Any]:
    out: dict[str, Any] = {"name": h.name, "address": str(h.address or ""), "last_connected": h.last_connected}
    if probe is not None:
        out |= {"reachable": probe.reachable, "herdr_installed": probe.installed, "session_running": probe.session}
        if probe.problem:
            out["problem"] = probe.problem
    return out


@mcp.tool(annotations=READ_ONLY)
async def list_tunnels(
    host: Annotated[str | None, Field(description="Only this host's (a name from list_hosts)")] = None,
) -> list[dict[str, Any]]:
    """The SSH tunnels the user set up on the Hosts page, which ssh on this Mac runs: name, type (local:
    a port on this Mac reaches target_host:target_port as the host sees it; remote: a port on the host
    reaches target_host:target_port as this Mac sees it; socks: a SOCKS proxy on this Mac through the host),
    and state (stopped, starting, running, reconnecting or failed), with a message saying why when it
    isn't running as it should."""
    return await _tunnels(host)


@mcp.tool()
async def start_tunnel(
    host: Annotated[str, Field(description="The host's name from list_hosts")],
    tunnel: Annotated[str, Field(description="The tunnel's name from list_tunnels")],
) -> dict[str, Any]:
    """Switches on one of the user's tunnels, as its switch on the Hosts page does: it keeps running, and
    reconnects when the connection drops, until stopped. Gives back its state; list_tunnels a few seconds
    later says whether it came up."""
    t = await _tunnel(host, tunnel)
    return await _page("POST", f"/api/hosts/{quote(host, safe='')}/tunnels/{t['id']}/start")


@mcp.tool()
async def stop_tunnel(
    host: Annotated[str, Field(description="The host's name from list_hosts")],
    tunnel: Annotated[str, Field(description="The tunnel's name from list_tunnels")],
) -> dict[str, Any]:
    """Switches off one of the user's tunnels: its ssh stops, and whatever used its port loses it."""
    t = await _tunnel(host, tunnel)
    return await _page("POST", f"/api/hosts/{quote(host, safe='')}/tunnels/{t['id']}/stop")


async def _tunnels(host: str | None) -> list[dict[str, Any]]:
    """The tunnels of the hosts the user shows, or of one of them."""
    if host is not None:
        await _shown(host)
    try:
        shown = {h.name for h in await service.shown_hosts()}
    except _PROBLEMS as e:
        raise ToolError(str(e)) from None
    found = await _page("GET", "/api/tunnels")
    return [t for t in found if t["host"] in shown and host in (None, t["host"])]


async def _tunnel(host: str, name: str) -> dict[str, Any]:
    found = await _tunnels(host)
    for t in found:
        if t["name"] == name:
            return t
    known = ", ".join(t["name"] for t in found) or "none"
    raise ToolError(f"{host} has no tunnel named {name!r}. Its tunnels are: {known}.")


async def _page(method: str, path: str) -> Any:
    try:
        return await page.call(method, path)
    except PageError as e:
        raise ToolError(str(e)) from None


def main() -> None:
    # Fail now on a bad database path, not on the first tool call.
    db.connect().close()
    mcp.run()
