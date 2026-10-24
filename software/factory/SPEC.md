# factory — spec and plan (draft)

A software factory for lo9i: you give it a task in a local repo, a team of
specialists on different models (Anthropic + OpenAI) debate it, a judge picks
the best option, you approve at checkpoints, and a coding agent implements it
in a git worktree. Result: a local branch.

## 1. Decisions

| Topic | Decision |
|---|---|
| Form | Plugin in lo9i-assistant-extras (public): MCP server + web page + SKILL.md, like watchlist |
| Location | `software/factory` (new category `software/factory` added to lo9i core) |
| Scope | Local repos only |
| Roles | Fixed for now: architect, implementer, reviewer, tester, security |
| Prompts | Defaults shipped per role, editable in the plugin's Settings page |
| Mode | Hybrid: debate via direct API calls; implementation by an agent in a worktree |
| Providers | Anthropic, OpenAI and GitHub Copilot; one provider:model per role, set in Settings |
| Agent | Claude Code or Codex CLI, chosen in Settings; Claude Code on an Anthropic key or on Copilot's Claude models (through a local relay, no key) |
| Discussion | Debate: proposals → cross-critique → revised proposals; max rounds (default 3, configurable) |
| Final word | A judge model picks the winner and sends it to the user for approval |
| Checkpoints | Spec, plan, final diff; approve or reject with a comment (redoes the stage) |
| Notifications | Web page + Telegram (through lo9i) |
| Concurrency | Several tasks in parallel, each in its own worktree |
| Output | Local branch `factory/<task-id>-<slug>`; never merges or pushes. The branch is the only result: a task asking for a review, an analysis or an answer gets a Markdown document written in the repo (the spec names its path); a build that changed nothing is reviewed as such, on what the agent said, and sent back unless the spec asks for no change |
| Limits | Max debate rounds and max cost (USD) per task, configurable; the task pauses when reached |

## 2. Pipeline

```
new task (repo, description)
 └─ SPEC stage: architect(s) propose → debate → judge → CHECKPOINT 1
 └─ PLAN stage: architect + security + tester propose → debate → judge → CHECKPOINT 2
 └─ BUILD stage: agent implements the plan in the worktree
      └─ tester runs the repo's test command
      └─ reviewer + security + tester critique the diff (debate)
      └─ judge: accept or send fixes back to the agent (up to N loops)
 └─ CHECKPOINT 3: final diff + test result + judge summary
 └─ done: branch left in the repo, worktree removed
```

Debate in each stage:
1. Each participating role writes a proposal (on its own model).
2. Each one reads the others' and critiques them.
3. Each one revises. Repeat 2–3 until judge sees agreement or max rounds.
4. Judge (its own model) picks/merges the best and writes the rationale.

Rejecting at a checkpoint: your comment is added to the context and the stage runs again.

## 3. Components

- **Engine** (`engine/`): stage state machine, debate loop, judge, cost meter.
- **Providers** (`providers/`): Anthropic, OpenAI and Copilot clients with token counting; price table for the direct APIs (Copilot counted as requests, not USD).
- **Agent runner** (`agents/`): starts Claude Code / Codex CLI headless in the worktree, streams its log.
- **Repo/worktrees** (`git.py`): `git worktree add` per task, branch naming, cleanup.
- **Store**: SQLite in `{data}/factory.db` — tasks, stages, messages (each proposal/critique), costs, settings, role prompts.
- **MCP tools** (for lo9i/chat): `new_task`, `list_tasks`, `task_status`, `approve`, `reject(comment)`, `cancel`, `settings`.
- **Web page**: task list; task view with stage timeline, the debate thread per round, judge decision, diff viewer, approve/reject buttons; Settings (models per role, judge model, agent, limits, prompts, per-repo test command).
- **Notifications**: at each checkpoint and at end/fail, notify lo9i so it arrives on Telegram with approve/reject.
- **Runner**: background worker in the MCP server process; tasks survive restarts (state in DB, resume from last finished step).

## 4. Settings

- Model per role + judge model (provider:model)
- Implementing agent: claude-code | codex
- Max debate rounds (3), max fix loops in build (2), max USD per task
- Max parallel tasks
- Prompt per role (default + override, "reset to default")
- Per repo: test command, base branch

Install fields (all optional, at least one): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, Copilot sign-in (GitHub device login, like lo9i core).

## 5. Plan (milestones)

1. **Skeleton**: server.yaml, pyproject, DB schema, settings, MCP tools stubs, empty web page. Tests.
2. **Providers + cost meter**: Anthropic, OpenAI, Copilot; price table, per-task budget enforcement.
3. **Debate engine + judge**: generic stage runner with fake providers in tests.
4. **Spec and plan stages + checkpoints** via MCP tools (approve/reject/redo).
5. **Worktrees + agent runner** (Claude Code first, then Codex) and build stage with test command and review loop.
6. **Web page**: task list, debate threads, diff, approve/reject, Settings and prompt editor.
7. **Notifications** through lo9i (web + Telegram) and checkpoint approve/reject from chat.
8. **Parallel tasks, resume after restart, cancel**, cleanup.
9. SKILL.md, README entry, version 1.0.0.

## 6. Testing decisions

- During build loops the tester writes tests for the change and runs only those (short run).
- The repo's full test command runs only at certain stages (at least before checkpoint 3; which others: TBD).
- Copilot: reuse lo9i's stored Copilot sign-in.

## 7. Agent communication

- Structured messages: proposal, risks, critique per other proposal, vote (JSON schema per message type).
- Every agent sees the full debate history of the stage.
- Agents can send directed questions to another agent; the addressee answers before the round closes.
- The judge decides when there is consensus and ends the debate (or max rounds is reached).
- The user can step in mid-debate with a comment; it is added to the thread and seen by all agents next turn.
- Review feedback to the implementer is a list of points; each must be marked resolved/declined with a reason.

## 8. UI

- Home: kanban board, one column per stage (spec, plan, build, review, done; plus waiting-for-you).
- Task view: summary first. A progress stepper (spec → plan → build → review & tests → final diff), a "now" sentence of what's happening (derived from saved state, no model call), and per stage its outcome in short (the judge's decision, or the latest proposals and open points). The debate stays available as a chat behind a per-stage toggle: one bubble per agent with role + model badge; directed questions shown as replies.
- Live: messages stream in real time (SSE/htmx).
- Checkpoint panel: judge decision + rationale, losing options expandable, approve/reject (with comment).
- Diff: summary (files, +/-) and a link to open the worktree/branch in the editor.
- Costs: per task, per stage and per agent (USD for direct APIs, requests for Copilot).
- Telegram: short summary with ✅/❌; ❌ asks for the comment.
- Comment box always available to step into the debate.

## 9. Details

- Editor link: VS Code by default (`vscode://file/<worktree>`), editor configurable in Settings.
- Full test run: always before the final checkpoint; also after build if the last full run took less than a configurable time (default 5 min).
- User comment mid-debate pauses the debate until the agents answer it.
- No limit on directed questions; the debate is still bounded by max rounds and the cost cap.
- Kanban: no drag for now; cards move only by approve/reject.
- Repo context (README, tree, CLAUDE.md/AGENTS.md) only when the task includes it.
- Claude Code on Copilot (`models.implementer` = `copilot:<Claude model>`): a relay on 127.0.0.1 per build signs its requests with lo9i's Copilot sign-in; no Anthropic key. Its requests are counted, not USD, and `--max-budget-usd` isn't set. In docker mode the container reaches the relay as `host.docker.internal` (Docker Desktop); Docker Engine on Linux needs local mode.

## 10. Data and state

- One SQLite database at `~/.lo9i/data/factory/factory.db`: tasks, stages, messages, decisions, costs.
- History kept forever; the user can delete a task (and its debate) from the UI.
- After the final diff is approved, the worktree is deleted; the branch `factory/<id>-<slug>` stays.
- On restart, tasks resume from the last saved step (every message/stage change is persisted before continuing).

## 11. Sandboxing

- Two run modes, chosen in Settings (per task override possible):
  - **Docker** (default): the implementer runs in a container with only the worktree mounted; any command and full network allowed.
  - **Local**: every command and every network access needs user approval (web/Telegram).
- Nothing outside the worktree is reachable (no ~/.lo9i, keys, other repos).
- No secrets/env vars passed to the agent for now.

## 12. Open

- Docker image per repo (default image vs repo Dockerfile) — decide in the build milestone.
- How the agent's own API key/login reaches the container without exposing other secrets.
