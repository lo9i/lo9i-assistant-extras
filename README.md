# lo9i-assistant-extras

Optional features for the [lo9i assistant](https://github.com/lo9i). Each one is a skill or an MCP
server that the assistant installs from this repository, so its core stays small.

| Folder | What |
|---|---|
| `mcp/<name>/` | An MCP server (`server.yaml`): its tools, the values it asks for, optionally a web page, and optionally its skill (`SKILL.md`, how to use its tools), which comes and goes with it |
| `skills/<name>/` | A skill on its own (`SKILL.md`, optionally `scripts/`), for features that need no server |
| `lib/<name>/` | A shared library plugins use, named in their `server.yaml` under `libraries:` and installed with them. `lib/lo9i-chat` is what the chat channels share: the assistant's channel API, how a run looks in a chat, approval and question buttons |

Install from the app: **Install** in `/skills` or `/mcp` (terminal), or on the Skills or MCP servers
page (web), with this repository's address. **Update** downloads a newer version later.

## What's here

- **expenses** (`mcp/expenses`): assets, recurring obligations and bills, in one currency you choose. Its
  tools, an **Expenses** page in the web app, and a skill that tells the assistant how to use them.
- **telegram** (`mcp/telegram`): chat with the assistant on Telegram, with voice notes, photos and
  documents. It asks for a bot token from [@BotFather](https://t.me/BotFather); pair your account by
  sending the bot the code the app shows.
- **slack** (`mcp/slack`): chat with the assistant in a Slack DM, and tools to search, read and post in
  your workspace as you (every post asks you first). Create the Slack app from
  [`manifest.json`](mcp/slack/manifest.json) and enter its three tokens.
- **gmail**, **microsoft**, **icloud**, **yahoo**, **aol**, **fastmail** (`mcp/<name>`): mail providers.
  Each one runs nothing: its `email:` section gives lo9i the provider's servers and how to get the
  password or sign in. Once one is installed, its accounts are added in its details in Plugins.

Every plugin has a `category:` (communications, finance or other) that groups it in Plugins; a
channel or a mail provider is in communications.

## Versions

Each plugin declares `version:` in its `server.yaml` (or a standalone skill in its `SKILL.md` header). The Plugins page shows the installed version and the newer one when there is one. Raise it with every change worth updating to.

## Developing a server

Each server folder, and each library under `lib/`, is its own uv project:

```
cd mcp/expenses
uv run pytest
```

A chat channel (`channel:` in `server.yaml`) is a process lo9i runs that talks to it over HTTP; the
protocol is in lo9i's `docs/channel-plugins.md`. Plugins depend on a library with a uv path dependency
on `../../lib/<name>`, which works both here and once installed.
