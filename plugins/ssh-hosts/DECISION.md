# ssh-hosts: herdr investigation and decision

Phase 1 of the plan. Nothing but this file was written. Tags: `docs` = read on herdr.dev or GitHub, `--help` = from
the binary's help, `observed` = seen by running it. herdr is not installed here, so nothing is `--help` or `observed`
and **no herdr property below counts as a pass**.

## 1. herdr (v0.9.5, released 2026-10-14; Apache-2.0; Rust; repo herdrdev/herdr, active, not archived)

| Question | Finding | Level |
|---|---|---|
| What is it | A terminal runtime for coding agents: a TUI with tmux-style panes, backed by a background server that keeps terminals alive and reattachable | docs |
| Interface | TUI + CLI + a socket API. No library | docs |
| Inventory | "Machine catalog" of opaque id, label, SSH target, remote session name, enabled flag. Targets are `~/.ssh/config` aliases or `ssh://user@host:port`. Catalog path not stated | docs |
| Auth | Delegated to OpenSSH; no passwords or keys stored. Background connections never prompt; passphrase keys need `ssh-add` | docs |
| Host keys | "Unchanged" OpenSSH checking. Unknown or changed key puts the machine in an Attention state. Whether it passes `StrictHostKeyChecking=yes` itself is not stated | docs |
| Non-interactive | `machine status/list --json`, `--machine X <cli-cmd>` run herdr's own CLI on the remote. Exit codes not documented. Nothing documents running an arbitrary command and getting stdout, stderr and exit code back separately | docs |
| Running commands | `pane run` / `pane.send_input` types into a PTY pane. The docs say responses "acknowledge queue admission, not command execution" | docs |
| Multi-host / transfer | Several machines in one window. No file transfer | docs |
| Daemon / socket | Yes. Unix socket `~/.config/herdr/herdr.sock` (or per-session). Newline-delimited JSON. Permissions and peer authentication not documented | docs |
| Own approval mode | None documented for herdr. It flags panes where an agent is blocked and `agent.send_keys` can answer them | docs |
| Connection reuse | Shared ControlMaster/ControlPersist 600; `remote.manage_ssh_config=true` manages it. Compression on by default. ForwardAgent follows the user's ssh config | docs |
| Remote install | `herdr machine add` can install or replace herdr on the remote, after a prompt | docs |
| Telemetry | Not mentioned (absence of mention, not a denial) | docs |
| ProxyJump | Not mentioned | docs |

Conclusion: herdr is a multiplexer for long-lived interactive agent sessions, not a command runner. It has no stdout/stderr/exit-code
contract and no per-call hook. Its shape (PTY panes, daemon, shared control connections, socket without documented auth,
remote installs) is the opposite of the gated, per-call model the spec asks for.

## 2. Repo facts (lo9i-assistant-extras)

- Layout: `<group>/<category>/<name>/` with `server.yaml` (name, version, format: 2, description, fields, steps,
  `trusted`, `command: "{bin}/<script>"`), `SKILL.md`, `pyproject.toml`, `uv.lock`, `.python-version`, `src/lo9i_<name>/`, `tests/`.
  Reference plugin: `media/video/youtube`; permission model: `phone`, `slack`, `calendar`.
- **Path problem:** the plan names `plugins/ssh-hosts/`, but the README says a plugin outside the category folders is never
  listed. Categories are a fixed list in lo9i (`core/repositories/categories.py`); none fits SSH except `other/general`.
  Proposal: build in `other/general/ssh-hosts` and delete this `plugins/ssh-hosts/` folder (moving this file).
  I could not confirm the category list because the lo9i checkout is not available here.
- Language: Python >=3.12, uv project, hatchling, MCP server over stdio (`mcp>=2.2.0`, `MCPServer`, `@mcp.tool()`),
  ruff (line length 120), pytest + pytest-asyncio (asyncio_mode=auto). Scoped test command: `cd <plugin> && uv run pytest`.
- **Confirmation API:** there is no in-plugin Confirmer. lo9i decides **before** the tool call: a tool without
  `ToolAnnotations(read_only_hint=True)` asks the user per call "unless they trust the plugin" (`trusted: true` in
  `server.yaml`). So confirmation is per call but happens in the host, and the host has an always-allow
  ("trust") mode that the plugin cannot see or disable. MCP elicitation inside a tool is not used anywhere in the repo and
  is unconfirmed in lo9i.
  Consequences for the plan's design: the Confirmer abstraction fails closed by default, with the host's per-call prompt as the real gate.
  The allowlist auto-approve can only be done by splitting tools: a `read_only_hint=True` tool that itself refuses anything
  not allowlisted and not safe, and a non-read-only tool for everything else. The plugin must ship `trusted: false` and
  document the always-allow residual risk.
- CI: none in the repo. The only gate is `.githooks/pre-commit`, which needs a lo9i checkout (`LO9I_DIR`); there is none here.
- Existing mentions of herdr or ssh in the repo: none.
- Environment for tests:
  - `docker`: not installed. `sshd`: not installed (`/usr/sbin/sshd` absent). `strace`: not installed. herdr: not installed.
  - OpenSSH client 9.2p1 (Debian 12), but **`ssh` cannot start in this container**: uid 501 has no passwd entry, so `ssh -V`
    exits 255 with "No user exists for uid 501". Fake-ssh tests work, but anything that runs the real client needs nss_wrapper or a
    user with a passwd entry.
  - Python here is 3.11; the plugin needs 3.12, so uv would have to download one.
  - Therefore the real-sshd layer cannot run here: those tests will skip visibly, and a local run with sshd is required before sign-off.

## 3. Spec items against herdr

| Spec item | herdr |
|---|---|
| Inventory from `~/.ssh/config` and plugin config | partly (reads ssh aliases; its catalog is its own and unspecified) |
| `list_hosts(tag?)` | does not (no tags) |
| `test_connection` with 4 outcomes | partly (`machine status --json`, outcomes not documented) |
| `run` with stdout, stderr, exit code, duration, truncation | does not |
| `run_many` | does not |
| `upload` / `download` | does not |
| Per-call confirmation | does not (no hook; socket input is unauthenticated per docs) |
| Allowlist and destructive guard | does not |
| Strict host keys | partly (unverified) |
| No local shell | unknown; PTY panes use a shell by design |
| Audit log without output | does not |
| ProxyJump | partly (inherits ssh config) |
| Timeout kills the process, stdin closed, no TTY | does not (PTY by design) |

## 4. Disqualifier checklist

| Disqualifier | Status | Level |
|---|---|---|
| Its own always-allow mode | none documented; `agent.send_keys` can answer a blocked agent's prompt | docs |
| Socket not 0600, or any listener | a daemon and a socket exist; permissions not documented | docs (fails: unverified) |
| Session reuse we cannot tear down | ControlPersist 600 and a background server; teardown not documented | docs (fails: unverified) |
| Secrets in argv or env | unknown | unverified |
| Non-strict host keys | not stated | unverified |
| Local shell in the command path | PTY pane shell by design | docs (fails) |
| stdout, stderr and exit code separable | not documented; PTY | docs (fails) |
| Telemetry | not mentioned | unverified |
| Forwarding on by default | ForwardAgent follows the user's config; the plugin cannot force it off through herdr | docs (fails) |
| PTY only | yes for commands | docs (fails) |

Several explicit fails and no verified pass. Option A (wrap herdr) would not reach its own enabling bar, so it would
fall back to OpenSSH anyway.

## 5. Recommendation

**Option B**: do not wrap herdr. Build the OpenSSH plugin (`ssh -G` check, forced options, argv without a local shell, confirmation, allowlist,
audit log, output caps) as an MCP plugin in the repo's Python style. The README warns that herdr sessions bypass this
plugin's confirmation and logging, and the plugin never spawns or reuses herdr sockets. Option C (herdr's inventory as an
extra source) is possible, but the catalog path and format are undocumented, so I advise against it for v1. Option D is
wrong: it would leave the assistant with no gated access.

Decisions I also need confirmed (they change the plan):
1. Folder: `other/general/ssh-hosts` instead of `plugins/ssh-hosts`.
2. Confirmation: the host's per-call prompt, with a split `run_readonly` (allowlisted only, read-only hint) and `run` (always asks) tool.
3. Real-sshd tests will skip here (no sshd, docker, strace, and ssh will not start without a passwd entry).
