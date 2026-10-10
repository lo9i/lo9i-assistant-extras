# lo9i-assistant-extras

Optional features for the [lo9i assistant](https://github.com/lo9i). Each one is a plugin that the
assistant installs from this repository, so its core stays small.

Plugins live in their category's folder, `<group>/<category>/<name>/`, which groups them in the app's
Plugins page. The categories are a fixed list in lo9i (`core/repositories/categories.py`):
`apps/finance`, `apps/media`, `assistant/agents`, `assistant/modes`, `communications/chat`, `communications/contacts`, `communications/email`, `communications/phone`, `media/video` and `other/general`.

A plugin folder holds one of:

- `server.yaml`: a plugin lo9i installs and, depending on its sections, runs: tools (an MCP server),
  a chat channel (`channel:`), mail provider presets (`email:`, runs nothing), a web page (`page:`).
  A `SKILL.md` next to it (how to use its tools) comes and goes with it.
- `SKILL.md` alone (optionally `scripts/`): a skill, for features that need no server.

A plugin that works only on some systems says so with `platforms:` (`linux`, `macos`) in its `server.yaml` or
`SKILL.md` header; lo9i doesn't list it anywhere else.

Plugins share no code: each one is complete in its folder. What lo9i does for them (running a chat, a
coding agent's tasks) is in lo9i, and they talk to it through the protocols in lo9i's `docs/`.

Install from the app: Plugins in the web app, `/plugins` in the terminal app. **Update** downloads a
newer version later.

## What's here

- **expenses** (`apps/finance/expenses`): assets, recurring obligations and bills, in one currency
  you choose. Its tools, an **Expenses** page in the web app, and a skill that tells the assistant how
  to use them.
- **watchlist** (`apps/media/watchlist`): the movies and TV shows you want to watch or are watching,
  and the YouTube channels you follow. Movies and shows come from TMDB, with posters, release dates
  and episodes; for each show it keeps the last episode you watched and counts the ones aired since.
  Channels are added from an @handle or a link, and their latest uploads are read from YouTube when
  asked. Its tools, a **Watchlist** page in the web app, and a skill. It asks for a free
  [TMDB](https://www.themoviedb.org/settings/api) API key.
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
- **contacts** (`communications/contacts/contacts`): the assistant looks people up in the Contacts on
  your Mac, by name, email or phone number: their numbers, emails, birthday and addresses. It only reads.
  Mac only, while the assistant runs in your login session.
- **phone** (`communications/phone/phone`): the assistant calls people with the Phone app on your Mac,
  through your iPhone, with their numbers from the contacts plugin. macOS asks you to confirm each call,
  and you do the talking. Mac only, while the assistant runs in your login session.
- **claude-code** (`assistant/agents/claude-code`): hand coding tasks to Claude Code, which works on
  them on its own in the repository you name and reports back. While the assistant's model is GitHub
  Copilot it runs on your Copilot plan, with nothing to enter; otherwise it takes a Claude token (from a
  Pro or Max plan, `claude setup-token`) or an Anthropic API key. The Claude Code CLI comes with the plugin.
- **copilot** (`assistant/agents/copilot`): the same with GitHub Copilot. It asks for a fine-grained
  GitHub token with the Copilot Requests permission, and downloads the Copilot CLI on its first task.
- **youtube** (`media/video/youtube`): download a YouTube video (MP4, up to 1080p unless you ask
  for more), its audio as an MP3 with tags and cover, or its transcript as a text file; or let the
  assistant read the transcript to summarize or answer questions about the video. Files go to the
  folder you enter, your Downloads folder by default. ffmpeg and the JavaScript runtime yt-dlp needs
  come with the plugin.
- **programmer** (`assistant/modes/programmer`): a skill for coding in your repositories: read the
  project's rules first, find a bug's cause before fixing it, tests first, review the diff, report what
  was verified. Pin it to a coding conversation with `/pin programmer`. Its debugging, testing and
  review steps (adapted from Hermes Agent's skills) are in `references/`, read when needed.

## Versions

Each plugin declares `version:` in its `server.yaml` (or a standalone skill in its `SKILL.md` header). The Plugins page shows the installed version and the newer one when there is one. Raise it with every change worth updating to.

## Developing a plugin

Each plugin folder with code, and each library, is its own uv project:

```
cd apps/finance/expenses
uv run pytest
```

A chat channel (`channel:` in `server.yaml`) is a process lo9i runs that talks to it over HTTP; the
protocol is in lo9i's `docs/channel-plugins.md`. A coding agent (`agent:`) is a worker lo9i runs for
each task, which reads the task from a folder and writes its steps and result there; the protocol is in
lo9i's `docs/agent-plugins.md`.
