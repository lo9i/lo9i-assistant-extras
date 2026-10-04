# lo9i-assistant-extras

Optional features for the [lo9i assistant](https://github.com/lo9i). Each one is a plugin that the
assistant installs from this repository, so its core stays small.

Plugins live in their category's folder, `<group>/<category>/<name>/`, which groups them in the app's
Plugins page. The categories are a fixed list in lo9i (`core/repositories/categories.py`):
`apps/finance`, `communications/chat`, `communications/email` and `other/general`.

A plugin folder holds one of:

- `server.yaml`: a plugin lo9i installs and, depending on its sections, runs: tools (an MCP server),
  a chat channel (`channel:`), mail provider presets (`email:`, runs nothing), a web page (`page:`).
  A `SKILL.md` next to it (how to use its tools) comes and goes with it.
- `SKILL.md` alone (optionally `scripts/`): a skill, for features that need no server.

`<group>/lib/<name>/` is a library the group's plugins share, named in their `server.yaml` under
`libraries:` and installed with them. `communications/lib/lo9i-chat` is what the chat channels share:
the assistant's channel API, how a run looks in a chat, approval and question buttons.

Install from the app: Plugins in the web app, `/plugins` in the terminal app. **Update** downloads a
newer version later.

## What's here

- **expenses** (`apps/finance/expenses`): assets, recurring obligations and bills, in one currency
  you choose. Its tools, an **Expenses** page in the web app, and a skill that tells the assistant how
  to use them.
- **telegram** (`communications/chat/telegram`): chat with the assistant on Telegram, with voice
  notes, photos and documents. It asks for a bot token from [@BotFather](https://t.me/BotFather); pair
  your account by sending the bot the code the app shows.
- **slack** (`communications/chat/slack`): chat with the assistant in a Slack DM, and tools to search,
  read and post in your workspace as you (every post asks you first). Create the Slack app from
  [`manifest.json`](communications/chat/slack/manifest.json) and enter its three tokens.
- **gmail**, **microsoft**, **icloud**, **yahoo**, **aol**, **fastmail** (`communications/email/<name>`):
  mail providers. Each one runs nothing: its `email:` section gives lo9i the provider's servers and how
  to get the password or sign in. Once one is installed, its accounts are added in its details in
  Plugins.
- **custom-email** (`communications/email/custom-email`): mail on any other server, such as your own
  domain or a work account. Its accounts take the servers you enter (SMTP host and port, IMAP host).
  Installing it also takes the accounts that were connected by hand before it existed.

## Versions

Each plugin declares `version:` in its `server.yaml` (or a standalone skill in its `SKILL.md` header). The Plugins page shows the installed version and the newer one when there is one. Raise it with every change worth updating to.

## Developing a plugin

Each plugin folder with code, and each library, is its own uv project:

```
cd apps/finance/expenses
uv run pytest
```

A chat channel (`channel:` in `server.yaml`) is a process lo9i runs that talks to it over HTTP; the
protocol is in lo9i's `docs/channel-plugins.md`. Plugins depend on a library with a uv path dependency
on `../../lib/<name>`, which works both here and once installed.
