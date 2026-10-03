# lo9i-assistant-extras

Optional features for the [lo9i assistant](https://github.com/lo9i). Each one is a skill or an MCP
server that the assistant installs from this repository, so its core stays small.

| Folder | What |
|---|---|
| `mcp/<name>/` | An MCP server (`server.yaml`): its tools, the values it asks for, optionally a web page, and optionally its skill (`SKILL.md`, how to use its tools), which comes and goes with it |
| `skills/<name>/` | A skill on its own (`SKILL.md`, optionally `scripts/`), for features that need no server |

Install from the app: **Install** in `/skills` or `/mcp` (terminal), or on the Skills or MCP servers
page (web), with this repository's address. **Update** downloads a newer version later.

## What's here

- **expenses** (`mcp/expenses`): assets, recurring obligations and bills, in one currency you choose. Its
  tools, an **Expenses** page in the web app, and a skill that tells the assistant how to use them.

## Developing a server

Each server folder is its own uv project:

```
cd mcp/expenses
uv run pytest
```
