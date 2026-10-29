# google-drive

The assistant searches and reads your Google Drive, and creates and edits Google Docs and Sheets, as your Google
account.

- **Search** your files (yours, shared with you, shared drives) by words in their name or text, kind, folder or
  date, and list a folder.
- **Read** a Google Doc (as Markdown), a Google Sheet (its tabs and cells), Google Slides, a PDF's text or a text
  file.
- **Docs**: create one from Markdown (headings, lists, tables, links become the Doc's formatting), add text at the
  end, replace a text everywhere in it, or rewrite it whole, keeping its name, folder, sharing and version history.
- **Sheets**: create one with its first rows, write cells (formulas work), add rows under a table, clear cells, add
  a tab.

Searching and reading never ask. Every change asks you first, unless you trust the plugin in Plugins. It never
deletes, moves or shares your files (a new file can go into a folder you name).

It signs in with a Google OAuth app of your own, as the gmail plugin's "sign in with Google" does: Google's review
would be needed for an app shared by everyone, and isn't for one only you use. Its tools are in
`src/lo9i_google_drive/` and how the assistant uses them in `SKILL.md`. It needs lo9i with plugin sign-ins (plugin
format 5).

## Setting it up

You need a Google Cloud project; it's free. If you made one for Gmail, use the same project and the same OAuth
client: add the redirect URI below to it if it isn't there.

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create a project (or pick yours).
2. **Turn on the APIs**: in *APIs & Services → Library*, enable the **Google Drive API**, the **Google Docs API**
   and the **Google Sheets API**.
3. **Consent screen**: in *Google Auth Platform* (*APIs & Services → OAuth consent screen*), set it up: an app name,
   your email, audience **External**. Then, under *Audience*, **Publish app** (In production). While an app is in
   Testing, Google ends its sign-ins after 7 days. Google doesn't need to verify an app only you use.
4. **OAuth client**: in *Clients* (*APIs & Services → Credentials*), create an **OAuth client ID** of type
   **Web application**. Under *Authorized redirect URIs*, add the one the plugin's setup shows: the web app's
   address followed by `/app/oauth`, like `https://your-mac.your-tailnet.ts.net:8765/app/oauth`; from the terminal
   app it's `http://localhost:8765/app/oauth`. Copy the **client ID** and the **client secret**.
5. **Install and sign in**: install google-drive in Plugins (web app) or `/plugins` (terminal app). Its setup
   shows the redirect URI: paste the client ID and secret and choose **Sign in with Google**. Google warns that the
   app isn't verified: choose *Advanced*, then go on, and allow access to your Drive. The setup then says which
   account it's signed in as.

Signing in again (for another account, or after Google ended the sign-in) is in the same place, and **Sign out**
forgets it. The assistant can also open the setup in a conversation.

## Problems

- *Not signed in to Google yet*: sign in in the plugin's setup.
- *Google refused the sign-in*: it ended (you revoked it in your Google Account, changed the client secret, or the
  app is still in Testing). Sign in again.
- *… API has not been used in project … or it is disabled*: enable that API (step 2); the error has its link.
- *redirect_uri_mismatch* on Google's page: the redirect URI registered in step 4 isn't exactly the one the setup
  shows (scheme, host, port and path).

## Scope

It asks for `https://www.googleapis.com/auth/drive` (with `openid email`, to show which account signed in): full
access to Drive, which reading any of your files and editing your Docs and Sheets need. The tools only do what's
listed above.

## Developing

```
uv run pytest
```

The tests talk to a fake Google (`tests/conftest.py`).
