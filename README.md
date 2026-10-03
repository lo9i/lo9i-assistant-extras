# lo9i-assistant-extras

Optional features for the [lo9i assistant](https://github.com/lo9i). Each one is a skill or an MCP
server that the assistant installs from this repository, so its core stays small.

| Folder | What |
|---|---|
| `skills/<name>/SKILL.md` | A skill: instructions the assistant follows, optionally with `scripts/` |
| `mcp/<name>/server.yaml` | An MCP server: its tools, the values it asks for, and optionally a web page |

Install from the app: **Install** in `/skills` or `/mcp` (terminal), or on the Skills or MCP servers
page (web), with this repository's address. **Update** downloads a newer version later.

## What's here

- **expenses**: assets, recurring obligations and bills, in Argentine pesos. The `expenses` MCP
  server has the tools and an **Expenses** page in the web app; the `expenses` skill tells the
  assistant how to use them.

## Developing a server

Each server folder is its own uv project:

```
cd mcp/expenses
uv run pytest
```
