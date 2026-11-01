---
name: youtube
description: Summarizing YouTube videos, and downloading them, their audio as MP3, and their transcripts (the youtube MCP server). Use it when the user shares a YouTube link and wants a summary of it, to know what the video says, the video, the audio or song, or the transcript.
---

The `youtube` MCP server's tools are `mcp__youtube__download_video`, `mcp__youtube__download_audio`, `mcp__youtube__get_transcript` and `mcp__youtube__download_transcript`. They take a video's URL (youtube.com, youtu.be, Shorts) or its ID.

- **The video**: `download_video`. It's MP4 at up to 1080p; pass `max_height` only when the user asks for another quality (720, 2160 for 4K).
- **The audio, a song, a podcast episode**: `download_audio`, an MP3 with the title, channel and cover.
- **A summary** ("summarize this video", "what's this about?", "is it worth watching?"): `get_transcript`, and write the summary from it; see below. Don't download anything.
- **What the video says** (a question about it, a quote, when something is said): `get_transcript`, and answer from it. With `timestamps`, each line says when it's said.
- **The transcript as a file**: `download_transcript`. Add `timestamps` if they want to see when each part is said.

Without `languages`, the transcript is in the language spoken in the video. When the user wants another language, pass its code (`["es"]`); if the video has no transcript in it, the error lists the ones it has, so offer those or translate it yourself. A transcript YouTube generated (`generated: true`) has no punctuation and mishears names: say so when quoting it.

## Summaries

`get_transcript` gives the title, channel and duration, and the transcript in sections of about five minutes, each starting with its time ([12:30]). Start the summary with the title, channel and duration, then what the video is about in two or three sentences, then its main points in order, each with the time it starts at so the user can jump there. Keep it short unless they ask for more detail; a video of a few minutes needs a few lines. Answer in the user's language, whatever the video's.

A long transcript comes in parts (`parts` is more than 1): call `get_transcript` again with `part` 2, 3… until you have them all, noting each part's main points as you go, before writing the summary. Tell the user when that will take a while (a video of several hours). If the video has no transcript, say so: you can't summarize it without one.

When the user shares a YouTube link without saying what they want, ask whether they want a summary or a download.

A download takes from seconds to a few minutes. When it's done, tell the user the file's name and folder: you can't send them the file. A playlist link without a video in it is refused; ask which video they mean. Download one video per request unless the user asks for several.
