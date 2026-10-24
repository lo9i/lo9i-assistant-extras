---
name: hosts
description: Working on the user's Linux hosts (servers, a Raspberry Pi, machines in their SSH config) with the hosts MCP server, in the same terminal the user sees on the Hosts page. Use it when the user asks about one of their hosts (is it up, disk space, a service, logs, updates) or to run a command on one.
---

The tools are in the `hosts` MCP server, named `mcp__hosts__<tool>`. If they aren't there, the server is turned off: the user turns it on in Plugins.

## The model

- **Hosts** are the user's SSH config's `Host` entries that they show on the Hosts page; `list_hosts` gives their names, which the other tools take. A host the user hid isn't there.
- Each host has one terminal for lo9i: a Herdr session named `lo9i` on that host, which keeps running when a connection drops. The user sees it on the Hosts page in the web app (Terminal). `run_command` types into a tab of its own there, labeled `assistant` (opened the first time, with a POSIX `sh` whatever the user's shell is), so the user sees every command and its output there, beside their own tabs, and their focus doesn't matter.

## How to work

1. `list_hosts` first: which hosts there are, whether each one answers now, has Herdr, and runs the session.
2. Read before you change: `read_screen` shows what your tab shows (`user_pane` for the pane the user has in front, when they ask about what they're looking at), and read-only commands (`uptime`, `df -h`, `systemctl status x`, `journalctl -u x -n 50`) find out what's going on. They need no OK.
3. **A command that changes the host needs the user's OK first**: installing or removing software, editing files, starting, stopping or restarting services, rebooting, deleting anything. Say which command you'll run on which host and why, and run it only once they agree, unless they asked for exactly that command. lo9i also asks them before each `run_command`.
4. Commands that wait for input (editors, pagers like `less`, `sudo` asking for a password) don't fit `run_command`: use `--no-pager`, `-y` or `| cat` where they help. One still running after `timeout_seconds` comes back with `still_running` and the screen so far; `read_screen` later shows how it went. When it asks for a password, ask the user to type it in the terminal on the Hosts page.
5. "Still running": a command of yours runs in your tab. Wait for it (`read_screen` shows how it goes), or ask the user to stop it in that tab.
6. "Herdr isn't installed": the user installs it from the host's terminal on the Hosts page (one button). "Can't reach": say why in plain words; the host may be off or on another network.

## Tunnels

The user sets up SSH tunnels on the Hosts page (New tunnel, or the host picked in the table, whose tunnels show on the right with their switches) or a host's terminal page (Tunnels, beside its terminal): ssh on this Mac forwards a port (local: a port on this Mac reaches one the host can reach; remote: a port on the host reaches one this Mac can reach; socks: a SOCKS proxy on this Mac through the host). Local and SOCKS ports listen on 127.0.0.1 only.

- `list_tunnels` says which there are and their state: running, starting, reconnecting (the connection dropped; it tries again on its own), failed (with a message saying what the user has to change), or stopped.
- `start_tunnel` / `stop_tunnel` switch one on or off, by host and tunnel name; lo9i asks the user first. A started tunnel takes a few seconds to come up: `list_tunnels` again to see.
- Only the user adds, changes or deletes tunnels, on the pages. "The Hosts page isn't running": the tunnels run in it, so ask the user to turn the hosts plugin off and on in Plugins.
