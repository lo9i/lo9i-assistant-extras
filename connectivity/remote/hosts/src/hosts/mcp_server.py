"""MCP server over stdio: the user's SSH hosts, and their Herdr session (herdr.py), the same terminal the
user sees on the Hosts page. lo9i starts it (see server.yaml) with HOSTS_DB set:

    HOSTS_DB=/path/hosts.db hosts-mcp
"""

import asyncio
from functools import cache
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import db, service
from .herdr import Herdr, HerdrError, Probe
from .service import Host, UnknownHostError
from .ssh import OpenSsh

INSTRUCTIONS = """\
The user's Linux hosts, from their SSH config, and a terminal on each that keeps running when the
connection drops (a Herdr session named lo9i). The user sees the same terminal on the Hosts page of
lo9i's web app, so a command run here shows there as if they had typed it.

Start with list_hosts. read_screen shows what the terminal shows. run_command types a command there
and returns its output and exit code. Commands that change a host (installing or removing software,
editing files, restarting services, rebooting, deleting anything) need the user's OK first: say what
you'll run and why, and run it only after they agree. Read-only commands (status, logs, disk space)
need no OK.
"""

mcp = MCPServer("hosts", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True)
# What a command may get at most to end before run_command gives back what it has so far.
_MAX_WAIT_SECONDS = 600


@cache
def _herdr() -> Herdr:
    return Herdr(OpenSsh())


async def _shown(name: str) -> Host:
    try:
        return await service.host(name, shown_only=True)
    except UnknownHostError as e:
        raise ToolError(str(e)) from None


@mcp.tool(annotations=READ_ONLY)
async def list_hosts(
    check: Annotated[bool, Field(description="Also connect to each one to see if it answers and runs Herdr")] = True,
) -> list[dict[str, Any]]:
    """The hosts the user shows on their Hosts page: name (what the other tools take), address
    (user@hostname:port) and when the user last opened its terminal. With check, also whether it
    answers now, has Herdr, and runs lo9i's Herdr session."""
    hosts = await service.shown_hosts()
    probes: list[Probe | None] = [None] * len(hosts)
    if check:
        probes = list(await asyncio.gather(*(_herdr().probe(h.name) for h in hosts)))
    return [_host(h, p) for h, p in zip(hosts, probes, strict=True)]


@mcp.tool()
async def run_command(
    host: Annotated[str, Field(description="The host's name from list_hosts")],
    command: Annotated[str, Field(description="A shell command line (bash or sh)")],
    timeout_seconds: Annotated[int, Field(ge=1, le=_MAX_WAIT_SECONDS, description="How long to wait for it")] = 60,
) -> dict[str, Any]:
    """Runs a command in the host's terminal, the Herdr session the user sees, by typing it there, and
    returns its output and exit code. A command that changes the host (installs, removes, edits files,
    restarts or stops services, reboots, deletes) needs the user's OK first: ask before calling this,
    unless they asked for exactly this command. Not for programs that wait for input (editors, pagers,
    password prompts): one still running after the timeout comes back with exit_code null and the screen
    so far; read_screen later shows how it went."""
    await _shown(host)
    try:
        run = await _herdr().run(host, command, timeout_seconds)
    except HerdrError as e:
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
) -> str:
    """What the host's terminal (the Herdr session the user sees) shows now, as text."""
    await _shown(host)
    try:
        return await _herdr().screen(host, lines)
    except HerdrError as e:
        raise ToolError(str(e)) from None


def _host(h: Host, probe: Probe | None) -> dict[str, Any]:
    out: dict[str, Any] = {"name": h.name, "address": str(h.address or ""), "last_connected": h.last_connected}
    if probe is not None:
        out |= {"reachable": probe.reachable, "herdr_installed": probe.installed, "session_running": probe.session}
        if probe.problem:
            out["problem"] = probe.problem
    return out


def main() -> None:
    # Fail now on a bad database path, not on the first tool call.
    db.connect().close()
    mcp.run()
