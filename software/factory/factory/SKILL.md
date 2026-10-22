---
name: factory
description: The software factory (the factory MCP server) — a team of models (architect, reviewer, tester, security, and a judge) that debates a coding task in one of the user's local git repositories, stops for the user's approval at the spec, the plan and the final diff, and has a coding agent (Claude Code or Codex) build it on a branch of its own. Use it when the user wants something built, fixed or changed in a repository by the factory, asks about a factory task, approves or rejects one, or answers what a task's agent asks; and when the factory's notices say a task waits for them.
---

The `factory` MCP server's tools are named `mcp__factory__<tool>`. If they aren't there, the server is turned off: the user turns it on in Plugins. The user also has the **Factory** page in the web app's sidebar: a board of the tasks, each task's debate as a chat, the diff, the costs, and Settings.

## A task's life

1. **Spec**: the architect writes what to build; the judge decides. The task waits: checkpoint 1.
2. **Plan**: the architect, security and tester each propose how, critique each other, revise and vote; the judge picks or merges the best. Checkpoint 2.
3. **Build**: the coding agent builds the plan in the task's own worktree, on the branch `factory/<id>-<slug>`; the change's own tests run, and the repository's full test command (when Settings has one for that repository).
4. **Review**: the reviewer, security and tester review the diff; the judge accepts it or sends points back to the agent, a few times at most. Checkpoint 3: the final diff.
5. **Done**: the branch stays in the repository. The factory never merges or pushes.

A task's `status` says where it is: `queued` and `running` (working), `waiting` (at a checkpoint, for the user), `paused` (a limit was reached or something failed: `reason` says what), `done`, `failed`, `cancelled`. Several tasks run at once.

## Starting a task

`new_task` with the repository's absolute path (`~` works) and the task as the user put it; it must be a git repository on this computer. For a **new project**, give a folder that doesn't exist yet (or an empty one), like `~/code/new-app`: the factory makes it a repository, and the task's branch starts empty. A folder with files that isn't a repository is refused: the user runs `git init` and commits them first. Keep the user's words: the team reads them. Set `include_context` when the team needs to know the repository (its README, file tree, CLAUDE.md/AGENTS.md) and the task doesn't say enough. Then say it started, and that the factory will tell them when the spec is ready.

## At a checkpoint

The factory's notices tell you when a task waits for the user. Then:

1. `task_status`: its `checkpoint.decision` has what the judge decided (`result`), why (`rationale`) and the options that lost. For the final diff, `changes` (files, lines added and deleted), `tests` (which passed; the output of those that failed), `review_points` (open ones especially) and `open_in_editor`.
2. Give the user the decision in a few lines, not the whole text: what it does, the one or two choices that matter, and for the diff the files, the tests and open points. On a chat channel keep it short; the whole text is on the Factory page.
3. Ask: ✅ approve or ❌ reject. On ❌, ask what to change, and call `reject` with it as the comment: the stage runs again with it. `approve` takes an optional comment.

Don't approve or reject on the user's behalf: it's their call, every time.

## While a task runs

- `comment`: what the user wants to tell the team mid-debate ("keep the old API"); every member answers it before the debate goes on. At a checkpoint, use `reject` with the comment instead.
- In local run mode, the agent asks before each command or network access: `task_status` lists them under `approvals`, and a notice says so. Tell the user what it wants to run, and answer with `answer_approval` (`allow`, and a `reason` when denying). The agent waits meanwhile.
- `paused`: say why (`reason`). Reached `max_usd`: the user raises it with `update_settings`, then `resume`. A missing key or a model without a price: the user fixes it (keys in Plugins), then `resume`.
- `cancel` stops a task, keeping its history and branch; `delete_task` removes it and its debate for good (the branch stays). Both ask the user first.

## Settings

`settings` shows the model of each role (`provider:model`: anthropic, openai or copilot), the agent, the limits, the run mode and which providers have a key; `update_settings` changes them by key, and `null` sets one back to its default. The ones users ask for:

- `max_usd`: what a task may spend; it pauses there. `max_parallel`: tasks at once. `max_rounds`: debate rounds.
- `models.<role>`, `agent` (`claude-code` or `codex`, which run Anthropic's and OpenAI's models: `models.implementer` must match), `run_mode` (`docker`, the default, or `local`).
- `repos.<path>`: a repository's `test_command` and `base_branch`. Offer to set the test command when a task's repository has none: without it only the change's own tests run.

Costs are in `task_status`: USD for the direct APIs, requests for GitHub Copilot, per stage and per role.

Starting, approving, rejecting and deleting ask the user for approval unless they trust the server.
