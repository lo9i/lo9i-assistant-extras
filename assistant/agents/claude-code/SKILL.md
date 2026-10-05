---
name: claude-code
description: Handing coding work in the user's repositories to Claude Code with the claude-code MCP server - bugs, features, refactors, reviews that need reading and changing a lot of code. Use it when the user asks for Claude Code, or for coding work too big to do well with bash here.
---

Claude Code is a coding agent. The `claude-code` MCP server's tools (`mcp__claude-code__<tool>`) hand it a task; it then reads the code, edits files and runs commands in that repository on its own, in the background, and reports. Each task asks the user before it starts.

## How to work

1. Find the repository's absolute path (ask the user if it isn't clear) and check it with `ls`.
2. Write the whole brief: Claude Code knows nothing of this conversation. Say the goal, the files or area involved, constraints (what not to touch, style, no commits unless asked), how to check the work (which tests to run), and what to report back.
3. `start_task` with the repository and the brief. It returns a task id at once.
4. Follow it with `task_status`. While the user waits for it, pass `wait_seconds` (up to 600) and call again until it ends. Otherwise tell the user it's running and check when they ask.
5. When it ends, tell the user what it changed and what it verified, in a few lines, and check the result yourself if it matters (`git -C <repo> diff --stat`, run the tests).
6. For changes to that work, `start_task` again with `follow_up_of` set to the task's id: it continues the same session and remembers what it did.

`stop_task` stops a running task and every command it started; what it already changed stays. `list_tasks` shows the latest ones.
