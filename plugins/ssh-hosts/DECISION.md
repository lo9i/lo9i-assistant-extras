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

The optional throwaway-sshd check was not run because herdr is not installed here (`command -v herdr` finds nothing), so
every herdr property above is unverified.

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
- Environment for tests, with the command run and what it printed (run as uid 501, `id` = `uid=501 gid=20(dialout)`):

  | Fact | Command | Output |
  |---|---|---|
  | No Docker | `docker info` | `/bin/bash: line 1: docker: command not found`. `command -v docker` prints nothing, exit 1 |
  | No sshd | `command -v sshd` ; `ls -l /usr/sbin/sshd` | nothing, exit 1 ; `ls: cannot access '/usr/sbin/sshd': No such file or directory` |
  | No strace | `command -v strace` | nothing, exit 1 |
  | No herdr | `command -v herdr` | nothing, exit 1 |
  | `ssh` will not start | `ssh -V` | `No user exists for uid 501`, exit 255 (uid 501 has no passwd entry) |
  | OpenSSH version | `dpkg -s openssh-client` ; `grep -E '^(PRETTY_NAME\|VERSION_ID)' /etc/os-release` | `Version: 1:9.2p1-2+deb12u10`, `Status: install ok installed` ; `Debian GNU/Linux 12 (bookworm)`. |
  | Python | `python --version` ; `python3 --version` ; `command -v uv` | `python: command not found` ; `Python 3.11.2` ; `/usr/local/bin/uv` |

  - OpenSSH 9.2p1 is the version dpkg reports for the installed client package; it was **not** read from `ssh -V`, which fails
    here. The real client has therefore never been run in this container, so no `ssh -G` or stderr format was observed.
  - Fake-ssh tests work. Anything that runs the real client needs nss_wrapper or a user with a passwd entry.
  - The plugin needs Python >=3.12 and `python3` here is 3.11.2, so uv would have to download one.
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

**Fallback, stated plainly.** If PB-1 cannot run (no lo9i checkout), or its item 1 or item 2 is unconfirmed, or the prompt is
not per call, the build uses the **single always-prompting tool**: no `read_only_hint`, no allowlist, no `run_readonly`.
Every command and every transfer goes through the host's prompt. The split `run_readonly` / `run` design applies only after
PB-1 passes. The user's choice in section 5 never enables the unprompted path; only a passed PB-1 does. If the host has no
per-call prompt at all, refuse everything and report it as a blocker. If item 3 shows elicitation exists, bring it back to
the user as a design choice; do not adopt it silently.

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

In this table `fail` means "cannot be shown to meet our bar". It does not mean "observed to fail". Rows marked
`fail (docs-inferred)` are inferred from herdr's documented design; none was observed, because herdr is not installed.

| Disqualifier | Status | Level | Note |
|---|---|---|---|
| Its own always-allow mode | unknown | docs | None documented, which is not a denial. `agent.send_keys` can type into any pane, so it may answer a blocked agent's prompt: a possible bypass path |
| A socket that is not 0600, or any listener | fail | docs | Fails on "any listener", not on a measured socket mode: a daemon and a Unix socket are documented, and the socket's permissions are unknown |
| Session reuse we cannot tear down | unknown | docs | ControlPersist 600 and a background server are documented; teardown is not |
| Secrets in argv or env | unknown | none | Nothing read about it |
| Non-strict host keys | unknown | docs | "Unchanged" OpenSSH checking is documented; `StrictHostKeyChecking=yes` is not stated |
| A local shell in the command path | fail (docs-inferred) | docs | Inferred: PTY panes run a shell by design |
| stdout, stderr and exit code separable | fail (docs-inferred) | docs | Inferred from the PTY design and "queue admission, not execution"; not observed |
| Telemetry | unknown | none | Not mentioned |
| Forwarding on by default | fail (docs-inferred) | docs | herdr does not force agent forwarding on: ForwardAgent follows the user's ssh config. The row fails because the plugin cannot force it off through herdr, not because herdr enables it |
| PTY only | fail (docs-inferred) | docs | Inferred: commands go to PTY panes |

Five rows fail, all on docs alone, and none passes. Four of the five are docs-inferred; the listener row fails on the
documented existence of a listener. Option A (wrap herdr) would not reach its own enabling bar, so it would fall back to
OpenSSH anyway.

## 5. Recommendation

**Option B**: do not wrap herdr. Build the OpenSSH plugin (`ssh -G` check, forced options, argv without a local shell,
confirmation, allowlist, audit log, output caps) as an MCP plugin in the repo's Python style. The plugin never spawns or
reuses herdr sockets. The README must say that herdr sessions may bypass this plugin's confirmation, forced options,
allowlist and audit log, and that they may reuse the ssh agent or forward it, which this plugin forces off; every such
line is worded "may", because none of it was observed. Option C (herdr's inventory as an extra source) is possible, but
the catalog path and format are undocumented, so I advise against it for v1. Option D is wrong: it would leave the
assistant with no gated access.

### Residual risk of the split `run_readonly` / `run` design (applies only after PB-1 passes)

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

### The one choice I need from you

Choose one of **A, B, C or D** (see the plan). My recommendation is **B**. Nothing is built until you reply.

Defaults that follow from this, and consequences you will see. These are not questions and need no answer:

- **Folder:** the plugin lives in `other/general/ssh-hosts`, because the repo's categories are a fixed list (README, unverified
  in lo9i, see 2b). `plugins/ssh-hosts/` is removed. PB-1 item 4 checks the category.
- **Tool split:** if PB-1 passes, `run_readonly` (allowlisted only, read-only hint) and `run` (always prompts). If PB-1 cannot
  run, or its items 1 or 2 are unconfirmed, or the prompt is not per call, the single always-prompting tool is built instead
  (section 2c). Your choice of A-D does not enable the unprompted path.
- **Skipped tests:** the real-sshd, strace and no-route-network tests skip visibly here (no sshd, docker or strace, and ssh will
  not start without a passwd entry). A local run with them is required before sign-off.

## 6. After the user chooses

1. **First step:** move this file to `other/general/ssh-hosts/DECISION.md`, then remove the `plugins/ssh-hosts/` folder, and
   remove `plugins/` only if it is empty. Never remove any other file.
2. Run PB-1. Stop and report if it changes the design.
3. Build test-first, with the scoped command `cd other/general/ssh-hosts && uv run pytest`.

Note for Phase 2 (tests):

- **Can run here with a fake-ssh binary:** the policy bypass table, Confirmer fail-closed, the tool schema (no skip field),
  frozen operation, output caps (limit-1, limit, limit+1, binary), log modes and the output canary, `run_many` semantics, and
  timeout with no orphan and stdin closed.
- **Option-injection tests** (a host or an argument starting with `-`) are aimed directly at `run_readonly`, as well as at the
  classifier and the other tools. They assert that no canary file is created and that nothing is spawned.
- **`list_tools` assertion:** `run`, `run_many`, `upload` and `download` carry no `read_only_hint`, in the style of
  `calendar/tests/test_mcp.py:26-31`. (In the single-tool fallback, the one tool carries none.)
- **Skip-only here:** the real-sshd layer (unknown and changed host keys, `StrictHostKeyChecking no` still rejects, `run_many`
  with one live and one dead host, transfer round trip), the strace checks, and the no-route-network check.
- Gate the skip-only tests behind a pytest marker (for example `real_sshd`) and an environment variable (for example
  `SSH_HOSTS_REAL_SSHD=1`), so that when they do not run they report as skipped with the reason shown (`-rs`), never as
  passed. A local run with real sshd, with that variable set and zero skips in that layer, is a hard sign-off condition. The
  same applies to the strace and no-route-network checks.
