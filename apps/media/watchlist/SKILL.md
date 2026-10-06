---
name: watchlist
description: Tracking the movies and TV shows the user wants to watch or is watching (how far they are in each show, new episodes, releases) and the YouTube channels they follow, with the watchlist MCP server. Use it for anything about what to watch, a show's episodes, adding a movie or show to the list, or new videos from their channels.
---

The data lives in the `watchlist` MCP server; its tools are named `mcp__watchlist__<tool>`. If those tools aren't there, the server is turned off: the user turns it on in Plugins.

## The model

- **Titles** are movies and shows (kind `movie` or `show`), with what TMDB says about them: poster, release date, genres, and for a show its seasons, the last episode aired and the next one.
- A title's **status** is `to_watch`, `watching`, `watched` or `dropped` (movies skip `watching`).
- For a show, `season` and `episode` are the **last episode watched**; `unwatched` counts the episodes aired since, and `next_to_watch` is the one after.
- **Channels** are YouTube channels. Their videos aren't stored: `new_uploads` reads them from YouTube when asked.

## How to work

1. Start with `summary`: shows with episodes to catch up on, episodes and movies coming out in the next 30 days, and counts.
2. Adding a movie or show: `search` (with `kind` when the user said which), pick the match (the year tells remakes apart), then `add_title` with its `tmdb_id` and `kind`. When several fit and nothing tells them apart, ask the user, listing them with their years.
3. "I watched the new episode of X": `watch_next`. "I'm on season 2, episode 4": `update_title` with `season: 2, episode: 4`. "Watched X" for a movie: `update_title` with `status: watched`.
4. Following a channel: `add_channel` with what the user gave, an @handle, a link to the channel, or a link to one of its videos. "What's new on my channels?": `new_uploads` (7 days by default; `days` to look further back).
5. Tool errors list the valid choices; correct the call instead of asking the user.

Changes ask the user for approval unless they trust the server. For browsing, the user has the **Watchlist** page in the web app's sidebar.

## With the youtube plugin

When the `youtube` server is there too, a video from `new_uploads` can be summarized from its transcript or downloaded with its tools: give them the video's `url`.

## Weekly digest

When the user wants a regular digest, offer a scheduled job (for example on Fridays at 19:00) whose prompt is: "Use the watchlist skill: send me the episodes I haven't watched yet, what comes out in the next week, and the new videos from my channels this week."
