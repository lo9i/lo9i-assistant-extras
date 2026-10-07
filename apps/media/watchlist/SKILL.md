---
name: watchlist
description: Tracking the movies and TV shows the user wants to watch or is watching (how far they are in each show, new episodes, releases) and the YouTube channels they follow, with the watchlist MCP server. Use it for anything about what to watch, a show's episodes, adding a movie or show to the list, or new videos from their channels; and when the user sends a picture of a movie or show (a poster, a screenshot of a post, a trailer or a streaming app) or a link to one (TMDB, IMDb, Letterboxd, a trailer), even without a word, since they want it on the list.
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
4. A picture or a link: see below.
5. Following a channel: `add_channel` with what the user gave, an @handle, a link to the channel, or a link to one of its videos. "What's new on my channels?": `new_uploads` (7 days by default; `days` to look further back).
6. Tool errors list the valid choices; correct the call instead of asking the user.

## From a picture or a link

The user sends these from their phone (Telegram, Slack) to add what they saw. A picture with nothing else, or with "add this", means: add it.

1. Read the title from the picture, and anything that tells it apart: the year, "a series", a season, the network or streaming service, the cast. A screenshot of a post can carry it in its caption.
2. A link names it directly. TMDB `themoviedb.org/movie/<id>` or `/tv/<id>`: `add_title` with that `tmdb_id` and kind (`tv` is a `show`). IMDb `imdb.com/title/tt…`: `add_title` with `imdb_id`. Letterboxd, a trailer or anything else: search by the title the link or its page shows.
3. `search` with the title, and with `kind` when the picture says (a season or episodes is a show; a release date "in theaters" is a movie).
4. One result fits what the picture shows: `add_title` it. Several fit and nothing in the picture tells them apart (a remake, a film and its series): ask with `ask_user`, one option per candidate as "Title (year, movie or series)", plus "None of these". No result fits, or the picture shows a scene with no title in it: say what you think it is and ask before adding.
5. Status is `to_watch` unless the user said otherwise ("I'm watching this": `watching`).
6. Answer in one line: what was added, its year, and the one fact that matters next, such as "out Dec 15" for a movie not out yet, or "S02E04 airs Oct 9" for a show on air. Already on the list (add_title says so): say so, with its status, instead of adding it again.

Changes ask the user for approval unless they trust the server. For browsing, the user has the **Watchlist** page in the web app's sidebar.

## With the youtube plugin

When the `youtube` server is there too, a video from `new_uploads` can be summarized from its transcript or downloaded with its tools: give them the video's `url`.

## Weekly digest

When the user wants a regular digest, offer a scheduled job (for example on Fridays at 19:00) whose prompt is: "Use the watchlist skill: send me the episodes I haven't watched yet, what comes out in the next week, and the new videos from my channels this week."
