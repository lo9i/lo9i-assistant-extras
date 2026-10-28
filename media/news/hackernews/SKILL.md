---
name: hackernews
description: Reading Hacker News (the hackernews MCP server) - the front page, Show HN, Ask HN, searching stories, and mentions of a word such as the user's project in stories and comments. Use it for anything about Hacker News or HN.
---

The tools are `mcp__hackernews__top_stories`, `mcp__hackernews__search` and `mcp__hackernews__mentions`. They only read; nothing can be posted or voted.

- **What's on HN** (the front page, Show HN, newest): `top_stories` with `kind` top, show, new, best or ask.
- **Stories about a topic**: `search`; `sort: "date"` and `days` for recent ones.
- **Who's talking about something** (the user's project, lo9i): `mentions`, which finds stories and comments.

Show each story as its title linked to its url, then points and comments, with the discussion link. For a comment, quote a short part and link it.
