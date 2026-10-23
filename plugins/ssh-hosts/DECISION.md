# ssh-hosts: herdr investigation and decision

Phase 1 of the plan. Nothing but this file was written.

Levels: `docs` = read on herdr.dev or GitHub, `--help` = from the binary's help, `observed` = seen by running it,
`none` = no evidence found. herdr is not installed here, so nothing is `--help` or `observed` and **no herdr property
below counts as a pass**. Status is `pass`, `fail` or `unknown`, kept apart from the level. A property that is only "not
mentioned" is `unknown`.

The lo9i checkout is not available here (`../lo9i` is absent, `LO9I_DIR` is unset). Every claim about lo9i's own
behaviour is therefore either cited to the file and line read in this repository, which only shows what plugin authors
wrote about lo9i, or marked unverified. See section 2b and the pre-build check PB-1.

## 1. herdr

Sources (all `docs`):

- Site: https://herdr.dev (version v0.9.5 in the page footer; license Apache 2.0; links to the repo and docs)
- Docs: https://herdr.dev/docs/
- Repo: https://github.com/herdrdev/herdr
- Release v0.9.5, tagged "Latest", published 14 Oct 16:07 by github-actions: https://github.com/herdrdev/herdr/releases/tag/v0.9.5.
  The page shows no year; 2026-10-14 comes from the matching preview build date on the same page.
- Not checked: whether the repo is archived, and the repo's own license file (the releases page shows no license text).

Language is Rust (docs). The version read is v0.9.5.

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
| Daemon / socket | Yes. Unix socket `~/.config/herdr/herdr.sock` (or per-session). Newline-delimited JSON. Permissions and authentication not documented | docs |
| Own approval mode | None documented. It flags panes where an agent is blocked, and `agent.send_keys` can type into a pane, so it may be able to answer a blocked agent's prompt (see section 4) | docs |
| Connection reuse | Shared ControlMaster/ControlPersist 600; `remote.manage_ssh_config=true` manages it. Compression on by default. ForwardAgent follows the user's ssh config | docs |
| Remote install | `herdr machine add` can install or replace herdr on the remote, after a prompt | docs |
| Telemetry | Not mentioned | none |
| ProxyJump | Not mentioned | none |

Conclusion: herdr is a multiplexer for long-lived interactive agent sessions, not a command runner. As documented, it has no
stdout/stderr/exit-code contract and no per-call hook. Its shape (PTY panes, daemon, shared control connections, a socket
whose authentication is not documented, remote installs) is the opposite of the gated, per-call model the spec asks for.
All of this is read from docs, not observed.

## 2. Repo facts (lo9i-assistant-extras)

### 2a. Verified by reading this repository

- Layout: `<group>/<category>/<name>/` with `server.yaml` (name, version, format: 2, description, fields, steps,
  `trusted`, `command: "{bin}/<script>"`), `SKILL.md`, `pyproject.toml`, `uv.lock`, `.python-version`, `src/lo9i_<name>/`, `tests/`.
  Reference plugin: `media/video/youtube`; permission model: `phone`, `slack`, `calendar`.
- Language: Python >=3.12, uv project, hatchling, MCP server over stdio (`mcp>=2.2.0`, `MCPServer`, `@mcp.tool()`),
  ruff (line length 120), pytest + pytest-asyncio (asyncio_mode=auto). Scoped test command: `cd <plugin> && uv run pytest`.
- Every plugin that reads declares `READ_ONLY = ToolAnnotations(read_only_hint=True)` and passes it as
  `@mcp.tool(annotations=READ_ONLY)`, for example `productivity/calendar/calendar/src/lo9i_calendar/mcp_server.py:32,54`;
  its changing tools use plain `@mcp.tool()` (same file, line 109). Tests assert the split from `list_tools`
  (`productivity/calendar/calendar/tests/test_mcp.py:26-31`, `communications/chat/slack/tests/test_mcp.py:44-45`).
- Nine `server.yaml` files set `trusted: false`; only `apps/media/watchlist/server.yaml:22` and
  `media/video/youtube/server.yaml:17` set `trusted: true`.
- No MCP elicitation anywhere in this repo: `grep -rni elicit` over the repository found no match outside this file.
- CI: none in the repo. The only gate is `.githooks/pre-commit`, which needs a lo9i checkout (`LO9I_DIR`); there is none here.
- Existing mentions of herdr or ssh in the repo: none.
- Environment for tests:
  - `docker`: not installed. `sshd`: not installed (`/usr/sbin/sshd` absent). `strace`: not installed. herdr: not installed.
  - OpenSSH client 9.2p1 (Debian 12), but **`ssh` cannot start in this container**: uid 501 has no passwd entry, so `ssh -V`
    exits 255 with "No user exists for uid 501". Fake-ssh tests work, but anything that runs the real client needs nss_wrapper or a
    user with a passwd entry.
  - Python here is 3.11; the plugin needs 3.12, so uv would have to download one.
  - Therefore the real-sshd layer cannot run here: those tests will skip visibly, and a local run with sshd is required before sign-off.

### 2b. lo9i claims: cited or unverified

None of these was checked in lo9i's code. "Cited" means a comment or test in this repository says it; that is evidence of
what authors believe, not of what lo9i does.

| Claim | Citation in this repo | Verified in lo9i code |
|---|---|---|
| The category list is fixed, and `other/general` is on it | `README.md:7-8` (names `core/repositories/categories.py`) | No. Unverified |
| A tool without the read-only hint asks the user per call unless the plugin is trusted | `productivity/calendar/calendar/server.yaml:24-25`, `communications/phone/phone/server.yaml:19`, `software/factory/factory/server.yaml:27-28`, `apps/finance/expenses/SKILL.md:24` | No. Unverified |
| `read_only_hint=True` skips the prompt | `communications/contacts/contacts/server.yaml:16`, `productivity/calendar/calendar/server.yaml:24` | No. Unverified |
| lo9i reads the hint from the annotations a server registers (not from names or elsewhere) | Tests read `tool.annotations.read_only_hint` from `list_tools` (above), which shows the plugins register it, not that lo9i reads it | No. Unverified |
| lo9i has no MCP elicitation a tool can use | Not used anywhere in this repo (grep above) | No. Unverified. Absence of use is not absence of support |
| The prompt is per call, and the host has an always-allow ("trust") mode the plugin cannot see or disable | `trusted:` in every `server.yaml`; the exact semantics are not stated | No. Unverified |

Design consequences if these hold (they stay assumptions until PB-1 passes):

- The Confirmer abstraction fails closed by default; the host's per-call prompt is the real gate.
- Allowlist auto-approve needs two tools: `run_readonly`, with `read_only_hint=True`, which itself refuses anything not
  allowlisted and safe; and `run`, without the hint, which always asks.
- The plugin ships `trusted: false` and documents the always-allow residual risk.

### 2c. Pre-build check PB-1 (must pass before any code is written)

Needs a lo9i checkout (`LO9I_DIR`, or `../lo9i`). Done in lo9i's code and against an existing plugin (calendar, or slack),
not from this repo's comments:

1. With a plugin at `trusted: false`, a tool with `read_only_hint=True` (calendar `list_events`) runs without a prompt,
   and a tool without it (calendar `create_event`, slack `slack_post`) prompts on every call. Confirm in the code that
   decides it, and by running it once.
2. That code reads the hint from the annotations registered through MCP `list_tools`, not from a name list or any other field.
3. Whether MCP elicitation exists in lo9i, and whether a tool could use it for its own per-call prompt.
4. That `other/general` is in `core/repositories/categories.py`, and that `python -m assistant.core.repositories.check` accepts the new folder.
5. Whether "trusted" can be turned on by the user later, and what it skips (all prompts, or only some).

**Fallback if 1 or 2 is not confirmed**, or if the prompt is not per call: build a single always-prompting tool with no
read-only hint and no auto-approve. The allowlist is dropped, there is no `run_readonly`, and every command, and every transfer, goes through
the host's prompt. If the host has no per-call prompt at all, refuse everything and report it as a blocker. If 3 shows
elicitation exists, bring it back to the user as a design choice; do not adopt it silently.

## 3. Spec items against herdr

| Spec item | herdr (all docs-level, none observed) |
|---|---|
| Inventory from `~/.ssh/config` and plugin config | partly (reads ssh aliases; its catalog is its own and unspecified) |
| `list_hosts(tag?)` | does not (no tags) |
| `test_connection` with 4 outcomes | partly (`machine status --json`, outcomes not documented) |
| `run` with stdout, stderr, exit code, duration, truncation | does not |
| `run_many` | does not |
| `upload` / `download` | does not |
| Per-call confirmation | does not (no hook; authentication of the socket is not documented) |
| Allowlist and destructive guard | does not |
| Strict host keys | unknown (unchanged OpenSSH checking; `StrictHostKeyChecking=yes` not stated) |
| No local shell | does not (PTY panes use a shell by design) |
| Audit log without output | does not |
| ProxyJump | unknown (not mentioned; it may inherit the ssh config, nothing says so) |
| Timeout kills the process, stdin closed, no TTY | does not (PTY by design) |

## 4. Disqualifier checklist

| Disqualifier | Status | Level | Note |
|---|---|---|---|
| Its own always-allow mode | unknown | docs | None documented, which is not a denial. `agent.send_keys` can type into any pane, so it may answer a blocked agent's prompt: a possible bypass path |
| A socket that is not 0600, or any listener | fail | docs | A daemon and a Unix socket exist (a listener). Its permissions are unknown |
| Session reuse we cannot tear down | unknown | docs | ControlPersist 600 and a background server are documented; teardown is not |
| Secrets in argv or env | unknown | none | Nothing read about it |
| Non-strict host keys | unknown | docs | "Unchanged" OpenSSH checking is documented; `StrictHostKeyChecking=yes` is not stated |
| A local shell in the command path | fail | docs | PTY pane shell by design |
| stdout, stderr and exit code separable | fail | docs | Inferred from the PTY design and "queue admission, not execution"; not observed |
| Telemetry | unknown | none | Not mentioned |
| Forwarding on by default | fail | docs | ForwardAgent follows the user's ssh config; the plugin cannot force it off through herdr |
| PTY only | fail | docs | Yes for commands |

Five rows fail on docs alone and none passes. Option A (wrap herdr) would not reach its own enabling bar, so it would
fall back to OpenSSH anyway.

## 5. Recommendation

**Option B**: do not wrap herdr. Build the OpenSSH plugin (`ssh -G` check, forced options, argv without a local shell, confirmation, allowlist,
audit log, output caps) as an MCP plugin in the repo's Python style. The README warns that herdr sessions bypass this
plugin's confirmation and logging, and the plugin never spawns or reuses herdr sockets. Option C (herdr's inventory as an
extra source) is possible, but the catalog path and format are undocumented, so I advise against it for v1. Option D is
wrong: it would leave the assistant with no gated access.

### Residual risk of decision 2 (split `run_readonly` / `run`)

- With `read_only_hint=True`, the host does not prompt for `run_readonly` (if PB-1 confirms it). The plugin's own allowlist
  and classifier are then the **only** gate for that tool. They must fail closed: any doubt, parse error, unknown host,
  or failed `ssh -G` check means refusal, never a fall-through to running.
- The bypass test table (metacharacters, newline and CR, glob, quotes, prefix abuse, `/bin/rm`, case variants, option
  injection) must target `run_readonly` directly, as well as the classifier.
- `run`, `run_many`, `upload` and `download` never carry `read_only_hint`. A test asserts it from `list_tools`, in the style of
  `calendar/tests/test_mcp.py:26-31`, so a later edit cannot silently remove their prompt.
- `server.yaml` ships `trusted: false`. If the user marks the plugin trusted, the host skips every prompt, including those for `run`
  and transfers. The plugin cannot detect or prevent that; the README documents it as a residual risk.
- The allowlist is a convenience, not a security boundary.

### Decisions I need confirmed (they change the plan)

1. Folder: `other/general/ssh-hosts` instead of `plugins/ssh-hosts`.
2. Confirmation: the host's per-call prompt, with a split `run_readonly` (allowlisted only, read-only hint) and `run`
   (always asks), subject to PB-1; if PB-1 fails, the single always-prompting tool.
3. Real-sshd tests will skip here (no sshd, docker, strace, and ssh will not start without a passwd entry).

## 6. After the user approves

1. **First step:** move this file to `other/general/ssh-hosts/DECISION.md` and remove the `plugins/ssh-hosts/` folder,
   and `plugins/` itself if it is then empty, so no stray plugin folder remains outside the category list.
2. Run PB-1. Stop and report if it changes the design.
3. Build test-first, with the scoped command `cd other/general/ssh-hosts && uv run pytest`.

Note for Phase 2 (tests): gate the real-sshd tests behind a pytest marker (for example `real_sshd`) and an environment
variable (for example `SSH_HOSTS_REAL_SSHD=1`), so that when they do not run, they report as skipped with the reason
shown (`-rs`), never as passed. A local run with real sshd, with that variable set and zero skips in that layer, is a hard
sign-off condition. The same applies to the strace and no-route-network checks.
