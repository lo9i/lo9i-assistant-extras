---
name: google-drive
description: The user's Google Drive, Docs and Sheets (the google-drive MCP server) — finding their files, reading a document, a spreadsheet, slides or a PDF from Drive, and creating or editing Google Docs and Sheets. Use it whenever the user mentions Drive, a Google Doc or Sheet, a docs.google.com or drive.google.com link, or a document or spreadsheet of theirs to find, read, summarize, write or update.
---

The `google-drive` MCP server's tools are named `mcp__google-drive__<tool>`: `search_files`, `read_file` and `read_sheet` only read; `create_document`, `append_to_document`, `replace_in_document`, `rewrite_document`, `create_spreadsheet`, `write_cells`, `append_rows`, `clear_cells` and `add_tab` change the user's files, and each asks the user for approval unless they trust the plugin. They act as the Google account the user signed in with.

## Finding and reading

1. **Find it**: `search_files` with a few words from the file's name or text (`query`), and `kind` when the user said what it is (`document`, `spreadsheet`, `presentation`, `pdf`, `folder`). Without a query it lists the latest changed files. A folder's files: `search_files` with only `folder` (`root` is My Drive). When several files fit, show them (name, kind, when changed) and ask which with `ask_user`.
2. **A link the user pastes** works as it is in every tool: no need to search.
3. **Read it**: `read_file`. A Doc comes as Markdown, a Sheet as its tabs' rows, Slides and PDFs as text. Long text comes in parts: when `more` says so, read on with `start` only if what you need isn't in what came. Scanned PDFs, images and Office files have no text here: give the user the link.
4. **A spreadsheet's cells**: `read_sheet` lists the tabs and their values; give `cells` (`'Budget'!A1:F40`) to read part of a big one.

## Writing Docs

- **New**: `create_document` with a title and `content` in Markdown (headings, lists, tables, links, bold), which becomes the Doc's formatting. `folder` puts it in a folder (find it with `search_files`, `kind: folder`).
- **Add to one**: `append_to_document` adds plain text as new paragraphs at the end.
- **Change words**: `replace_in_document` replaces every occurrence of an exact text, keeping its formatting. Read the Doc first and copy the text exactly; the answer says how many it changed (0 means it wasn't found as written).
- **Rewrite**: `rewrite_document` replaces the whole Doc with new Markdown, keeping its name, place, sharing and version history. Use it for big edits: read the Doc first and keep everything that should stay. Formatting Markdown can't express (colours, fonts, comments) is lost.

## Writing Sheets

- **New**: `create_spreadsheet` with a title and `rows` (a header row first) to start it with.
- **Add rows**: `append_rows` adds them under the last row of a tab's table (the first tab unless `tab` is named): logs, expenses, lists.
- **Set cells**: `write_cells` writes rows from the top left of `cells` over what's there. Values are taken as typed: `=SUM(B2:B10)` is a formula, `1500` a number, `2026-10-29` a date. Read the cells first when overwriting.
- **Empty cells**: `clear_cells`. **New tab**: `add_tab`.
- Name tabs in ranges with quotes: `'Q4 budget'!A1:D10`.

After creating or changing a file, answer in one line with what changed and its link.

## Problems

- "Not signed in to Google yet": open its setup with `open_plugin_setup` (`google-drive`) in the web or terminal app, where the user signs in; on a chat channel, ask them to sign in from Plugins in the app. Never ask for a client secret or password in the chat.
- "Google refused the sign-in": it ended (revoked, or the OAuth app is still in Testing, which ends sign-ins after 7 days). The user signs in again in the plugin's setup.
- An API "has not been used in project … or it is disabled": the user enables that API (Drive, Docs or Sheets) in their Google Cloud project; the error has the link.
- "found no such file": a wrong id, or a file the signed-in account can't see. Search again.
